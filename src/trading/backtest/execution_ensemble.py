"""固定した研究条件を seed × cost scenario で反復し、執行仮定への感度を集計する。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from threading import Event, Lock, current_thread, main_thread
from typing import TextIO
from uuid import uuid4

from trading.backtest.costs import STRESS_SCENARIOS
from trading.backtest.engine import ENGINE_VERSION
from trading.backtest.research import (
    broker_label,
    parse_param_override,
    warmup_days,
    with_param_overrides,
)
from trading.backtest.run import git_state, synthetic_usdjpy_spec
from trading.config import ENVIRONMENTS, load_config
from trading.strategy.registry import STRATEGIES

INPUT_FIELDS = ("dataset_hash", "feature_dataset_hash", "swap_dataset_hash")
MONEY_FIELDS = (
    "initial_equity", "realized_pnl", "unrealized_pnl", "net_pnl",
    "final_equity", "max_drawdown", "carry_total",
)
COUNT_FIELDS = (
    "fills", "trades", "unpriced_rollovers", "open_positions_at_end",
    "pending_commands_at_end",
)
HALT_FIELDS = (
    "daily_loss_halt_pct", "rolling_24h_loss_halt_pct", "high_water_mark_drawdown_halt_pct",
)
INTERPRETATION = (
    "固定した履歴と執行モデルの仮定に対する感度であり、将来の損失確率ではない。"
    "scenario 間は混合しない。期末の未決済ポジションは既存エンジンの Bid/Ask 時価評価を使い、"
    "強制決済費用と期間終了後のリスクを含まない。"
)
_CREATE_SUSPENDED = 0x00000004


class _WindowsJob:
    """親の強制終了時にも研究の子を終了させる、継承不可のジョブ。"""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class ThreadEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        self._thread_entry_type = ThreadEntry32
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self._kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
        self._kernel32.Thread32First.restype = wintypes.BOOL
        self._kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
        self._kernel32.Thread32Next.restype = wintypes.BOOL
        self._kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenThread.restype = wintypes.HANDLE
        self._kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        self._kernel32.ResumeThread.restype = wintypes.DWORD
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            if not self._kernel32.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error()) from error
            raise error
        # 成功した生の HANDLE は CloseHandle せず、親プロセス終了まで OS に保持させる。
        # 子へは継承しないため、強制終了でも最後のハンドルが閉じられる。

    def assign(self, process: subprocess.Popen) -> None:
        import ctypes

        if not self._kernel32.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def resume(self, process: subprocess.Popen) -> None:
        import ctypes

        # Popen が閉じた主スレッドのハンドルを、停止中の子の PID から取得し直す。
        snapshot = self._kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = self._thread_entry_type()
            entry.dwSize = ctypes.sizeof(entry)
            found = self._kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = self._kernel32.OpenThread(
                        0x0002, False, entry.th32ThreadID,  # THREAD_SUSPEND_RESUME
                    )
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        count = self._kernel32.ResumeThread(thread)
                        if count == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        if count != 1:
                            raise OSError(f"unexpected initial thread suspend count: {count}")
                    finally:
                        if not self._kernel32.CloseHandle(thread):
                            raise ctypes.WinError(ctypes.get_last_error())
                    return
                entry.dwSize = ctypes.sizeof(entry)
                found = self._kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            error = ctypes.get_last_error()
            if error != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(error)
            raise OSError(f"no thread found for suspended process {process.pid}")
        finally:
            if not self._kernel32.CloseHandle(snapshot):
                raise ctypes.WinError(ctypes.get_last_error())


def _start_trial(
    command: list[str], *, stdout: TextIO, stderr: TextIO,
    env: dict[str, str], job: _WindowsJob | None,
) -> subprocess.Popen:
    options = {"creationflags": _CREATE_SUSPENDED} if job is not None else {}
    process = subprocess.Popen(
        command, stdout=stdout, stderr=stderr, env=env, **options,
    )
    try:
        if job is not None:
            job.assign(process)
            job.resume(process)
    except BaseException:
        process.kill()
        process.wait()
        raise
    return process


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.5)


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} must be a JSON object")
    return value


def _probability(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("quantile must be a decimal in (0, 1]") from error
    if not number.is_finite() or not 0 < number <= 1:
        raise argparse.ArgumentTypeError("quantile must be a decimal in (0, 1]")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nearest_rank(values: list[Decimal], probability: Decimal) -> Decimal:
    """昇順の ceil(n*p) 番目（1始まり）。補間しない経験分位点。"""
    rank = int((len(values) * probability).to_integral_value(rounding=ROUND_CEILING))
    return sorted(values)[rank - 1]


def _metrics(summary: dict, symbol: str) -> dict[str, str]:
    if summary.get("symbol") != symbol:
        raise ValueError("summary symbol mismatch")
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        raise TypeError("summary metrics are missing")
    amounts: dict[str, Decimal] = {}
    for field in MONEY_FIELDS:
        value = metrics.get(field)
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a decimal string")
        try:
            amount = Decimal(value)
        except InvalidOperation as error:
            raise ValueError(f"invalid {field}") from error
        if not amount.is_finite():
            raise ValueError(f"{field} must be finite")
        amounts[field] = amount
    for field in COUNT_FIELDS:
        value = metrics.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
            raise ValueError(f"{field} must be a non-negative integer string")
    if amounts["initial_equity"] <= 0 or amounts["max_drawdown"] < 0:
        raise ValueError("invalid equity or drawdown")
    if amounts["net_pnl"] != amounts["realized_pnl"] + amounts["unrealized_pnl"]:
        raise ValueError("net_pnl does not match realized plus unrealized PnL")
    if amounts["final_equity"] != amounts["initial_equity"] + amounts["net_pnl"]:
        raise ValueError("final_equity does not match initial equity plus net_pnl")
    if int(metrics["fills"]) == 0 and (
        any(int(metrics[field]) for field in ("trades", "open_positions_at_end"))
        or any(amounts[field] != 0 for field in ("net_pnl", "max_drawdown", "carry_total"))
    ):
        raise ValueError("no-fill trial has positions, trades or nonzero PnL/costs")
    if int(metrics["unpriced_rollovers"]):
        raise ValueError("unpriced_rollovers: swap costs are incomplete")
    if int(metrics["pending_commands_at_end"]):
        raise ValueError("pending_commands_at_end: execution has not completed")
    return {field: metrics[field] for field in (*MONEY_FIELDS, *COUNT_FIELDS)}


def _validate_trial(
    trial: dict, expected: dict, baseline: dict | None,
) -> tuple[dict, dict[str, str]]:
    out = Path(trial["out"])
    stdout = _read_json(out / "stdout.json")
    if not isinstance(stdout.get("run_dir"), str):
        raise TypeError("research did not return run_dir")
    run_dir = Path(stdout["run_dir"]).resolve()
    if run_dir.parent != out.resolve():
        raise ValueError("run_dir is outside the trial output directory")
    trial["run_dir"] = str(run_dir)
    manifest = _read_json(run_dir / "manifest.json")
    if manifest.get("run_id") != run_dir.name:
        raise ValueError("manifest run_id mismatch")
    for field, value in {**expected, "seed": trial["seed"], "scenario": trial["scenario"]}.items():
        if field not in manifest or manifest[field] != value:
            raise ValueError(f"manifest mismatch: {field}")
    for field in INPUT_FIELDS:
        if not isinstance(manifest.get(field), str) or not re.fullmatch(
            r"[0-9a-f]{64}", manifest[field]
        ):
            raise ValueError(f"missing or invalid input fingerprint: {field}")
    if type(manifest.get("tick_count")) is not int or manifest["tick_count"] <= 0:
        raise ValueError("tick_count must be a positive integer")
    common = {
        key: value for key, value in manifest.items()
        if key not in {"run_id", "created_at", "seed", "scenario"}
    }
    metrics = _metrics(_read_json(run_dir / "summary.json"), expected["symbol"])
    common["initial_equity"] = metrics["initial_equity"]
    if baseline is not None and common != baseline:
        differing = sorted(key for key in common.keys() | baseline.keys()
                           if common.get(key) != baseline.get(key))
        raise ValueError(f"trial input mismatch: {', '.join(differing)}")
    return common, metrics


def summarize(plan: dict, trials: list[dict]) -> dict:
    scenarios = {}
    for scenario in plan["scenarios"]:
        group = [trial for trial in trials if trial["scenario"] == scenario]
        success = [trial for trial in group if trial["status"] == "succeeded"]
        complete = len(success) == len(group)
        distribution = None
        if complete:
            pnl = [Decimal(trial["metrics"]["net_pnl"]) for trial in success]
            drawdown = [Decimal(trial["metrics"]["max_drawdown"]) for trial in success]
            losses = sum(value < 0 for value in pnl)
            distribution = {
                "sample_count": len(pnl),
                "loss_trials": losses,
                "loss_fraction": str(Decimal(losses) / Decimal(len(pnl))),
                "lower_net_pnl": str(nearest_rank(pnl, Decimal(plan["pnl_quantile"]))),
                "upper_max_drawdown": str(
                    nearest_rank(drawdown, Decimal(plan["drawdown_quantile"]))
                ),
                "no_fill_trials": sum(int(t["metrics"]["fills"]) == 0 for t in success),
                "no_closed_trade_trials": sum(
                    int(t["metrics"]["trades"]) == 0 for t in success
                ),
                "open_position_trials": sum(
                    int(t["metrics"]["open_positions_at_end"]) > 0 for t in success
                ),
            }
        scenarios[scenario] = {
            "status": "complete" if complete else "incomplete",
            "planned": len(group),
            "succeeded": len(success),
            "failed": sum(t["status"] == "failed" for t in group),
            "running": sum(t["status"] == "running" for t in group),
            "not_started": sum(t["status"] == "planned" for t in group),
            "distribution": distribution,
        }
    return {
        "status": "complete" if all(s["status"] == "complete" for s in scenarios.values())
        else "incomplete",
        "interpretation": INTERPRETATION,
        "risk_mode": plan["risk_mode"],
        "purpose": plan["purpose"],
        "risk_basis": plan["risk_basis"],
        "loss_halts_pct": {field: plan["config"]["risk"][field] for field in HALT_FIELDS},
        "quantile_method": "nearest rank: sorted values[ceil(n * p) - 1], no interpolation",
        "pnl_quantile": plan["pnl_quantile"],
        "drawdown_quantile": plan["drawdown_quantile"],
        "amount_currency": plan["amount_currency"],
        "scenarios": scenarios,
    }


def run_ensemble(plan: dict, out: Path, *, research_dsn: str, max_parallel: int = 1) -> dict:
    """計画を先に保存する。強制停止時にも未着手・実行中の試行を残す。"""
    child_env = {**os.environ, plan["config"]["storage"]["dsn_env"]: research_dsn}
    out.mkdir(parents=True, exist_ok=False)
    _write_json(out / "plan.json", plan)
    trials = [dict(trial, status="planned") for trial in plan["trials"]]
    baseline = None
    job = _WindowsJob() if sys.platform == "win32" else None

    def checkpoint() -> dict:
        summary = summarize(plan, trials)
        _write_json(out / "results.json", {"trials": trials, "common_inputs": baseline})
        _write_json(out / "summary.json", summary)
        return summary

    def record_success(trial: dict, repro: dict | None = None) -> None:
        nonlocal baseline
        if trial["returncode"] != 0:
            raise ValueError(f"research exited with code {trial['returncode']}; see stderr.log")
        common, metrics = _validate_trial(trial, plan["expected_manifest"], baseline)
        # 逐次は従来どおり検証後、並列は親が完了を回収した時点の Git 状態を使う。
        if (git_state() if repro is None else repro) != plan["git_state"]:
            raise ValueError("git state changed during the ensemble")
        if baseline is None:
            baseline = common
        trial.update(status="succeeded", metrics=metrics)

    checkpoint()
    if max_parallel > 1:
        processes: dict[int, subprocess.Popen] = {}
        futures: dict[Future[int | None], int] = {}
        finished: dict[int, dict | None] = {}
        stopping = Event()
        process_lock = Lock()
        next_trial = 0
        next_result = 0

        def run_trial(index: int) -> int | None:
            trial = trials[index]
            trial_out = Path(trial["out"])
            with (trial_out / "stdout.json").open("w", encoding="utf-8") as stdout, (
                trial_out / "stderr.log"
            ).open("w", encoding="utf-8") as stderr:
                # 子の生成途中に親が割り込まれても、終了対象への登録を完了させる。
                with process_lock:
                    if stopping.is_set():
                        return None
                    process = _start_trial(
                        trial["command"], stdout=stdout, stderr=stderr, env=child_env, job=job,
                    )
                    processes[index] = process
                return process.wait()

        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            interrupted = False
            try:
                while next_result < len(trials):
                    while len(futures) < max_parallel and next_trial < len(trials):
                        index = next_trial
                        next_trial += 1
                        trial = trials[index]
                        trial["status"] = "running"
                        checkpoint()
                        print(f"{trial['scenario']} seed={trial['seed']}",
                              file=sys.stderr, flush=True)
                        try:
                            trial_out = Path(trial["out"])
                            trial_out.mkdir()
                            futures[pool.submit(run_trial, index)] = index
                        except (OSError, TypeError, ValueError) as error:
                            trial.update(status="failed", error=str(error))
                            finished[index] = None
                            checkpoint()
                    if futures:
                        done, _ = wait(futures, return_when=FIRST_COMPLETED)
                        for future in done:
                            index = futures.pop(future)
                            trial = trials[index]
                            try:
                                trial["returncode"] = future.result()
                                # Git は親で完了時に採取し、先行試行の待機中の変更と区別する。
                                finished[index] = git_state() if trial["returncode"] == 0 else None
                            except (OSError, TypeError, ValueError) as error:
                                trial.update(status="failed", error=str(error))
                                finished[index] = None
                            with process_lock:
                                processes.pop(index, None)
                        checkpoint()
                    # 検証を試行順に進め、最初に成功した試行を baseline にする。
                    while next_result in finished:
                        trial = trials[next_result]
                        repro = finished.pop(next_result)
                        if trial["status"] != "failed":
                            try:
                                record_success(trial, repro)
                            except (OSError, TypeError, ValueError) as error:
                                trial.update(status="failed", error=str(error))
                        next_result += 1
                        checkpoint()
            except KeyboardInterrupt:
                interrupted = True
                raise
            finally:
                # 再度の Ctrl-C で kill・回収・保存が飛ばされないよう、終了処理を守る。
                on_main_thread = current_thread() is main_thread()
                if on_main_thread:
                    previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
                try:
                    stopping.set()
                    for future in futures:
                        future.cancel()
                    with process_lock:
                        remaining = list(processes.values())
                        if interrupted:
                            for index, trial in enumerate(trials):
                                if trial["status"] == "running":
                                    if index in processes or "returncode" in trial:
                                        trial.update(status="failed", error="interrupted")
                                    else:
                                        trial["status"] = "planned"
                    for process in remaining:
                        if process.poll() is None:
                            process.terminate()
                    for process in remaining:
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    checkpoint()
                finally:
                    if on_main_thread:
                        signal.signal(signal.SIGINT, previous_sigint)
        return checkpoint()
    for trial in trials:
        trial_out = Path(trial["out"])
        trial_out.mkdir()
        trial["status"] = "running"
        checkpoint()
        print(f"{trial['scenario']} seed={trial['seed']}", file=sys.stderr, flush=True)
        try:
            # 長期 replay の stderr はファイルへ流し、メモリへ蓄積しない。
            with (trial_out / "stdout.json").open("w", encoding="utf-8") as stdout, (
                trial_out / "stderr.log"
            ).open("w", encoding="utf-8") as stderr:
                process = _start_trial(
                    trial["command"], stdout=stdout, stderr=stderr, env=child_env, job=job,
                )
                try:
                    trial["returncode"] = process.wait()
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
            record_success(trial)
        except (OSError, TypeError, ValueError) as error:
            trial.update(status="failed", error=str(error))
        except KeyboardInterrupt:
            trial.update(status="failed", error="interrupted")
            checkpoint()
            raise
        checkpoint()
    return checkpoint()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="backtest", choices=ENVIRONMENTS)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--dsn-env", required=True, help="研究用 DB の DSN を持つ環境変数名")
    parser.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    parser.add_argument("--from", dest="start", required=True, type=broker_label)
    parser.add_argument("--to", dest="end", required=True, type=broker_label)
    parser.add_argument("--seeds", nargs="+", required=True, type=int)
    parser.add_argument("--scenarios", nargs="+", required=True, choices=sorted(STRESS_SCENARIOS))
    parser.add_argument(
        "--max-parallel", type=_positive_int, default=1, metavar="N",
        help="同時実行する試行数（既定: 1、逐次）。完了時間は ceil(試行数 / N) 波で決まるので、"
             "波数が減らない N は資源競合を増やすだけになる（6 試行なら 4・5 は 3 と同じ 2 波）。"
             "Windows VPS 実測（2026 年 7 月、range_edge_reversal、1 本 416 万 tick）では、"
             "4 並列で 3.43 倍、1 本あたりの所要時間は約 16%% 増えた。"
             "live 収集と同居するホストで並列実行すると、収集の取り込みが遅れる。"
             "研究の子を Idle 優先度にしても改善しなかった。"
             "PostgreSQL 側の競合が疑われるが、原因は未特定。"
             "小さい N から実測して上げる。"
             "live 収集や MT5 と同居する場合はコア数未満にし、"
             "研究対象の過去区間へのバックフィルを同時に実行しない。",
    )
    parser.add_argument("--param", action="append", default=[], type=parse_param_override)
    parser.add_argument("--warmup-days", type=warmup_days, default=None)
    parser.add_argument("--pnl-quantile", type=_probability, default=Decimal("0.05"))
    parser.add_argument("--drawdown-quantile", type=_probability, default=Decimal("0.95"))
    parser.add_argument("--purpose", required=True, help="実験の目的（事前に固定して保存）")
    parser.add_argument("--risk-basis", required=True, help="使用する Risk 設定の根拠")
    parser.add_argument("--risk-mode", choices=("research", "operational-limits"), default="research")
    parser.add_argument("--out", default="reports/execution-ensembles")
    args = parser.parse_args()
    if args.start >= args.end:
        parser.error("--from must be earlier than --to")
    if len(set(args.seeds)) != len(args.seeds) or len(set(args.scenarios)) != len(args.scenarios):
        parser.error("seeds and scenarios must each be unique")
    if not args.purpose.strip() or not args.risk_basis.strip():
        parser.error("--purpose and --risk-basis must not be blank")
    if args.pnl_quantile > Decimal("0.5") or args.drawdown_quantile < Decimal("0.5"):
        parser.error("lower PnL quantile must be <= 0.5; upper drawdown quantile must be >= 0.5")

    research_dsn = os.environ.get(args.dsn_env)
    if not research_dsn:
        parser.error(f"research DSN environment variable {args.dsn_env!r} is not set")
    config = load_config(args.env)
    if args.strategy not in config.strategies:
        parser.error(f"config has no strategy {args.strategy!r}")
    config = with_param_overrides(config, args.strategy, dict(args.param))
    symbol = args.symbol or config.market.primary_instruments[0]
    strategy_config = config.strategies[args.strategy]
    if symbol != "USDJPY" or symbol not in strategy_config.instruments:
        parser.error("research requires the USDJPY dataset spec and a matching strategy instrument")
    if args.risk_mode == "operational-limits" and (
        not config.risk.trading_enabled
        or any(not 0 < getattr(config.risk, field) < 100 for field in HALT_FIELDS)
    ):
        parser.error("operational-limits requires enabled trading and loss halts strictly between 0 and 100%")
    strategy = STRATEGIES[args.strategy]
    warmup = timedelta(days=args.warmup_days) if args.warmup_days is not None else strategy.warmup(
        strategy_config
    )
    repro = git_state()
    if repro["git_commit"] == "unknown" or repro["git_dirty"] is None:
        parser.error("git state is unavailable; reproducibility cannot be verified")
    expected = {
        **repro,
        "environment": args.env,
        "symbol": symbol,
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.strategy_version,
        "engine_version": ENGINE_VERSION,
        "param_overrides": dict(args.param),
        "resolved_parameters": dict(strategy_config.params_for(symbol).values),
        "period_from": args.start.isoformat(),
        "period_to": args.end.isoformat(),
        "warmup_days": warmup / timedelta(days=1),
        "broker_server_ahead_of_ny_hours": config.market.broker_server_ahead_of_ny_hours,
        "config_sha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
        "python_version": sys.version.split()[0],
    }
    out = Path(args.out).resolve() / str(uuid4())
    command = [
        sys.executable, "-m", "trading.backtest.research", "--env", args.env,
        "--symbol", symbol, "--strategy", args.strategy,
        "--from", args.start.isoformat(), "--to", args.end.isoformat(),
    ]
    if args.warmup_days is not None:
        command += ["--warmup-days", str(args.warmup_days)]
    for key, value in args.param:
        command += ["--param", f"{key}={value}"]
    trials = []
    for scenario in args.scenarios:
        for seed in args.seeds:
            trial_out = out / f"{len(trials) + 1:04d}-{scenario}-seed-{seed}"
            trials.append({
                "scenario": scenario, "seed": seed, "out": str(trial_out),
                "command": [*command, "--scenario", scenario, "--seed", str(seed), "--out", str(trial_out)],
            })
    plan = {
        "schema_version": 1,
        "dsn_source_env": args.dsn_env,
        "created_at": datetime.now(UTC).isoformat(),
        "purpose": args.purpose,
        "risk_basis": args.risk_basis,
        "risk_mode": args.risk_mode,
        "interpretation": INTERPRETATION,
        "amount_currency": synthetic_usdjpy_spec(symbol).quote_currency.value,
        "git_state": repro,
        "expected_manifest": expected,
        "config": config.model_dump(mode="json"),
        "cost_models": {
            name: asdict(replace(STRESS_SCENARIOS[name], latency_ms=config.simulator.latency_ms))
            for name in args.scenarios
        },
        "seeds": args.seeds,
        "scenarios": args.scenarios,
        "pnl_quantile": str(args.pnl_quantile),
        "drawdown_quantile": str(args.drawdown_quantile),
        "trials": trials,
    }
    summary = run_ensemble(plan, out, research_dsn=research_dsn, max_parallel=args.max_parallel)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"ensemble_dir": str(out), **summary}, ensure_ascii=False, indent=2))
    if summary["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
