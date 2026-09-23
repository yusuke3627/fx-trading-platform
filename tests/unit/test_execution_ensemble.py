"""実行計画・失敗の保持と、scenario ごとの Decimal 分布。"""
import ctypes
import io
import json
import sys
from ctypes import wintypes
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trading.backtest import execution_ensemble as ensemble

REPRO = {"git_commit": "a" * 40, "git_dirty": False}


@pytest.mark.parametrize("failure", [None, "create", "configure", "assign", "close"])
def test_windows_job_checks_api_results_and_keeps_successful_handle(monkeypatch, failure):
    # POSIX の c_ulong は 64bit の場合があるが、Windows の DWORD / BOOL は常に 32bit。
    monkeypatch.setattr(wintypes, "DWORD", ctypes.c_uint32)
    monkeypatch.setattr(wintypes, "BOOL", ctypes.c_int32)
    kernel32 = Mock()
    handle = 1 << 40
    kernel32.CreateJobObjectW.return_value = 0 if failure == "create" else handle
    kernel32.SetInformationJobObject.return_value = failure not in {"configure", "close"}
    kernel32.AssignProcessToJobObject.return_value = failure != "assign"
    kernel32.CloseHandle.return_value = failure != "close"
    load_dll = Mock(return_value=kernel32)
    monkeypatch.setattr(ctypes, "WinDLL", load_dll, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "synthetic API error"),
                        raising=False)
    process = SimpleNamespace(_handle=handle + 1)
    if failure:
        with pytest.raises(OSError, match="synthetic API error"):
            ensemble._WindowsJob().assign(process)
    else:
        job = ensemble._WindowsJob()
        job.assign(process)
        assert job._handle == handle
    load_dll.assert_called_once_with("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.assert_called_once_with(None, None)
    assert kernel32.CreateJobObjectW.argtypes == [ctypes.c_void_p, wintypes.LPCWSTR]
    assert kernel32.CreateJobObjectW.restype is ctypes.c_void_p
    assert kernel32.SetInformationJobObject.argtypes == [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    assert kernel32.SetInformationJobObject.restype is wintypes.BOOL
    assert kernel32.AssignProcessToJobObject.argtypes == [wintypes.HANDLE, wintypes.HANDLE]
    assert kernel32.AssignProcessToJobObject.restype is wintypes.BOOL
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert kernel32.CloseHandle.restype is wintypes.BOOL
    if failure != "create":
        job_handle, info_class, pointer, size = kernel32.SetInformationJobObject.call_args.args
        assert job_handle == handle
        assert info_class == 9
        limits = pointer._obj
        basic = type(limits.BasicLimitInformation)
        extended = type(limits)
        assert limits.BasicLimitInformation.LimitFlags == 0x2000
        if ctypes.sizeof(wintypes.HANDLE) == 8:
            assert ctypes.sizeof(basic) == 64
            assert [getattr(basic, name).offset for name, _ in basic._fields_] == [
                0, 8, 16, 24, 32, 40, 48, 56, 60,
            ]
            assert [getattr(extended, name).offset for name, _ in extended._fields_] == [
                0, 64, 112, 120, 128, 136,
            ]
            assert size == ctypes.sizeof(extended) == 144
        else:
            assert ctypes.sizeof(basic) == 48
            assert [getattr(basic, name).offset for name, _ in basic._fields_] == [
                0, 8, 16, 20, 24, 28, 32, 36, 40,
            ]
            assert [getattr(extended, name).offset for name, _ in extended._fields_] == [
                0, 48, 96, 100, 104, 108,
            ]
            assert size == ctypes.sizeof(extended) == 112
    if failure in {None, "assign"}:
        kernel32.AssignProcessToJobObject.assert_called_once_with(handle, handle + 1)
    else:
        kernel32.AssignProcessToJobObject.assert_not_called()
    if failure in {"configure", "close"}:
        kernel32.CloseHandle.assert_called_once_with(handle)
    else:
        kernel32.CloseHandle.assert_not_called()


def test_cli_writes_utf8_to_a_redirected_cp932_stream(monkeypatch, tmp_path):
    fake_research(monkeypatch)
    buffer = io.BytesIO()
    stdout = io.TextIOWrapper(buffer, encoding="cp932")
    monkeypatch.setattr(sys, "stdout", stdout)
    invoke(monkeypatch, tmp_path, ["--purpose", "感度確認 🧪"])
    stdout.flush()
    assert stdout.encoding == "utf-8"
    summary = json.loads(buffer.getvalue().decode("utf-8"))
    assert summary["purpose"] == "感度確認 🧪"
    assert summary["status"] == "complete"


@pytest.mark.parametrize("failures", [0, 1, 5])
def test_write_json_retries_permission_error_without_losing_previous_file(
    monkeypatch, tmp_path, failures,
):
    path = tmp_path / "results.json"
    path.write_text('{"status": "running"}', encoding="utf-8")
    payload = {"status": "完了 🧪"}
    replace = Path.replace
    attempts = []
    sleep = Mock()

    def replace_after_unlock(temporary, target):
        attempts.append(temporary)
        assert target == path
        assert json.loads(temporary.read_text(encoding="utf-8")) == payload
        assert json.loads(path.read_text(encoding="utf-8")) == {"status": "running"}
        if len(attempts) <= failures:
            raise PermissionError("synthetic file lock")
        return replace(temporary, target)

    monkeypatch.setattr(Path, "replace", replace_after_unlock)
    monkeypatch.setattr(ensemble.time, "sleep", sleep)
    ensemble._write_json(path, payload)
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert not path.with_suffix(".tmp").exists()
    assert len(attempts) == failures + 1
    assert sleep.call_count == failures
    assert [call.args[0] for call in sleep.call_args_list] == [0.5] * failures


@pytest.mark.parametrize(("error_type", "attempts"), [(PermissionError, 6), (OSError, 1)])
def test_write_json_propagates_final_error(monkeypatch, tmp_path, error_type, attempts):
    path = tmp_path / "results.json"
    path.write_text('{"status": "running"}', encoding="utf-8")
    errors = [error_type(f"synthetic failure {index}") for index in range(attempts)]
    replace = Mock(side_effect=errors)
    sleep = Mock()
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(ensemble.time, "sleep", sleep)
    with pytest.raises(error_type) as raised:
        ensemble._write_json(path, {"status": "complete"})
    assert raised.value is errors[-1]
    assert replace.call_count == attempts
    assert sleep.call_count == attempts - 1
    assert [call.args[0] for call in sleep.call_args_list] == [0.5] * (attempts - 1)
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "running"}


@pytest.mark.parametrize("error", [OSError("assignment failed"), KeyboardInterrupt()])
def test_failed_job_assignment_kills_and_reaps_child(monkeypatch, error):
    process = Mock()
    job = Mock()
    job.assign.side_effect = error
    monkeypatch.setattr(ensemble.subprocess, "Popen", Mock(return_value=process))
    with pytest.raises(type(error)) as raised:
        ensemble._start_trial(["synthetic"], stdout=None, stderr=None, env={}, job=job)
    assert raised.value is error
    job.assign.assert_called_once_with(process)
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with()


@pytest.mark.parametrize("error", [OSError("wait failed"), KeyboardInterrupt(), SystemExit(9)])
def test_serial_wait_failure_kills_and_reaps_child(monkeypatch, tmp_path, error):
    process = Mock()
    process.wait.side_effect = [error, -9] * 6
    monkeypatch.setattr(ensemble, "_start_trial", Mock(return_value=process))
    with pytest.raises(SystemExit if isinstance(error, OSError) else type(error)):
        invoke(monkeypatch, tmp_path)
    expected = 6 if isinstance(error, OSError) else 1
    assert process.kill.call_count == expected
    assert process.wait.call_count == 2 * expected


def invoke(monkeypatch, tmp_path, extra=()):
    monkeypatch.setattr(ensemble, "git_state", lambda: REPRO.copy())
    monkeypatch.setenv("ENSEMBLE_FIXTURE_DSN", "synthetic-only")
    monkeypatch.setenv("TRADING_DB_DSN", "must-not-use-inherited-runtime-db")
    monkeypatch.setattr(sys, "argv", [
        "execution_ensemble", "--dsn-env", "ENSEMBLE_FIXTURE_DSN",
        "--strategy", "failed_spike_reversal",
        "--from", "2026-01-05T00:00:00+00:00", "--to", "2026-01-06T00:00:00+00:00",
        "--seeds", "11", "22", "33", "--scenarios", "normal", "spread_x2",
        "--purpose", "合成入力による感度確認", "--risk-basis", "研究のため損失停止を緩和",
        "--out", str(tmp_path / "path with spaces"), *extra,
    ])
    ensemble.main()


def artifacts(tmp_path):
    out = next((tmp_path / "path with spaces").iterdir())
    return tuple(json.loads((out / name).read_text(encoding="utf-8")) for name in (
        "plan.json", "results.json", "summary.json",
    ))


def fake_research(monkeypatch, mutate=None):
    calls = []

    def run(command, *, stdout, stderr, env, job):
        assert env["TRADING_DB_DSN"] == "synthetic-only"
        assert command[:3] == [sys.executable, "-m", "trading.backtest.research"]
        out = Path(command[command.index("--out") + 1])
        plan = json.loads((out.parent / "plan.json").read_text(encoding="utf-8"))
        saved = json.loads((out.parent / "results.json").read_text(encoding="utf-8"))
        # 全試行の計画は子プロセス起動より前に保存される。
        assert len(saved["trials"]) == 6
        assert saved["trials"][len(calls)]["status"] == "running"
        trial = plan["trials"][len(calls)]
        calls.append(command)
        run_dir = out / "run"
        run_dir.mkdir()
        manifest = {
            **plan["expected_manifest"], "run_id": "run", "created_at": "any",
            "seed": trial["seed"], "scenario": trial["scenario"],
            "tick_count": 100,
            **{field: "a" * 64 for field in ensemble.INPUT_FIELDS},
        }
        pnl = Decimal(("-1.1", "0", "2.2", "-9.9", "-3.3", "1.1")[len(calls) - 1])
        metrics = {
            "initial_equity": "1000000", "realized_pnl": str(pnl), "unrealized_pnl": "0",
            "net_pnl": str(pnl), "final_equity": str(Decimal(1000000) + pnl),
            "max_drawdown": str(len(calls)), "carry_total": "0", "fills": "2",
            "trades": "1", "unpriced_rollovers": "0", "open_positions_at_end": "0",
            "pending_commands_at_end": "0",
        }
        if mutate:
            code = mutate(len(calls), manifest, metrics, out)
            if code is not None:
                stderr.write("synthetic failure\n")
                return SimpleNamespace(wait=lambda: code)
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (run_dir / "summary.json").write_text(json.dumps({"symbol": "USDJPY", "metrics": metrics}), encoding="utf-8")
        stdout.write(json.dumps({"run_dir": str(run_dir)}))
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(ensemble, "_start_trial", run)
    return calls


def test_cli_keeps_scenarios_separate_and_records_fixed_assumptions(monkeypatch, tmp_path):
    calls = fake_research(monkeypatch)
    invoke(monkeypatch, tmp_path, ["--param", "cooldown_seconds=50", "--warmup-days", "3"])
    plan, results, summary = artifacts(tmp_path)
    assert summary["status"] == "complete"
    assert len(calls) == 6
    assert len(results["trials"]) == 6
    assert plan["config"]["risk"]["daily_loss_halt_pct"] == "100.0"
    assert plan["risk_mode"] == "research"
    assert plan["dsn_source_env"] == "ENSEMBLE_FIXTURE_DSN"
    assert "synthetic-only" not in json.dumps(plan)
    assert plan["purpose"] == "合成入力による感度確認"
    assert plan["risk_basis"] == "研究のため損失停止を緩和"
    assert plan["cost_models"]["spread_x2"]["spread_multiplier"] == 2.0
    assert plan["expected_manifest"]["warmup_days"] == 3
    for command in calls:
        assert command[command.index("--param") + 1] == "cooldown_seconds=50"
        assert command[command.index("--warmup-days") + 1] == "3.0"
    normal = summary["scenarios"]["normal"]["distribution"]
    stress = summary["scenarios"]["spread_x2"]["distribution"]
    assert normal["loss_fraction"] == str(Decimal(1) / 3)
    assert stress["loss_fraction"] == str(Decimal(2) / 3)
    assert normal["lower_net_pnl"] == "-1.1"
    assert normal["upper_max_drawdown"] == "3"
    assert stress["lower_net_pnl"] == "-9.9"
    assert stress["upper_max_drawdown"] == "6"
    assert "将来の損失確率ではない" in summary["interpretation"]


@pytest.mark.parametrize("field", [
    "git_commit", "config_sha256", "period_from", "strategy_id", "strategy_version",
    "engine_version", "symbol", "warmup_days", "resolved_parameters", "seed", "scenario",
    "dataset_hash", "feature_dataset_hash", "swap_dataset_hash", "tick_count",
])
def test_manifest_mismatch_withholds_incomplete_scenario(monkeypatch, tmp_path, field):
    def mutate(index, manifest, metrics, out):
        if index == 2:
            manifest[field] = "b" * 64 if field in ensemble.INPUT_FIELDS else "different"
    fake_research(monkeypatch, mutate)
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, tmp_path)
    assert error.value.code == 1
    _, results, summary = artifacts(tmp_path)
    assert summary["status"] == "incomplete"
    assert summary["scenarios"]["normal"]["distribution"] is None
    assert summary["scenarios"]["normal"]["succeeded"] == 2
    assert summary["scenarios"]["normal"]["failed"] == 1
    assert summary["scenarios"]["spread_x2"]["status"] == "complete"
    assert field in results["trials"][1]["error"]


@pytest.mark.parametrize(("field", "value"), [
    ("unpriced_rollovers", "1"), ("pending_commands_at_end", "1"),
    ("max_drawdown", "NaN"), ("net_pnl", "Infinity"), ("max_drawdown", "-1"),
    ("net_pnl", "12.34"), ("final_equity", "12.34"), ("trades", "-1"),
    ("net_pnl", 12.34), ("max_drawdown", None), ("fills", "2.5"),
])
def test_invalid_or_incomplete_metrics_fail_the_trial(monkeypatch, tmp_path, field, value):
    def mutate(index, manifest, metrics, out):
        if index == 1:
            metrics[field] = value
    fake_research(monkeypatch, mutate)
    with pytest.raises(SystemExit, match="1"):
        invoke(monkeypatch, tmp_path)
    _, results, summary = artifacts(tmp_path)
    assert results["trials"][0]["status"] == "failed"
    assert summary["scenarios"]["normal"]["distribution"] is None


def test_child_failure_is_recorded_and_later_trials_still_run(monkeypatch, tmp_path):
    calls = fake_research(monkeypatch, lambda index, *_: 7 if index == 1 else None)
    with pytest.raises(SystemExit, match="1"):
        invoke(monkeypatch, tmp_path)
    _, results, summary = artifacts(tmp_path)
    assert len(calls) == 6
    assert results["trials"][0]["returncode"] == 7
    assert "code 7" in results["trials"][0]["error"]
    assert summary["scenarios"]["normal"]["distribution"] is None


def test_interruption_preserves_all_planned_trials(monkeypatch, tmp_path):
    def mutate(index, *_):
        if index == 2:
            raise KeyboardInterrupt
    fake_research(monkeypatch, mutate)
    with pytest.raises(KeyboardInterrupt):
        invoke(monkeypatch, tmp_path)
    _, results, summary = artifacts(tmp_path)
    assert [t["status"] for t in results["trials"]] == [
        "succeeded", "failed", "planned", "planned", "planned", "planned",
    ]
    assert summary["status"] == "incomplete"
    assert summary["scenarios"]["spread_x2"]["not_started"] == 3


def test_no_fills_and_open_positions_remain_explicit_samples(monkeypatch, tmp_path):
    def mutate(index, manifest, metrics, out):
        if index == 2:
            metrics.update(fills="0", trades="0", max_drawdown="0")
        if index == 3:
            metrics.update(realized_pnl="0", unrealized_pnl=metrics["net_pnl"],
                           fills="1", trades="0", open_positions_at_end="1")
    fake_research(monkeypatch, mutate)
    invoke(monkeypatch, tmp_path)
    _, _, summary = artifacts(tmp_path)
    normal = summary["scenarios"]["normal"]["distribution"]
    assert normal["sample_count"] == 3
    assert normal["no_fill_trials"] == 1
    assert normal["no_closed_trade_trials"] == 2
    assert normal["open_position_trials"] == 1
    assert normal["loss_trials"] == 1


@pytest.mark.parametrize("extra", [
    ["--seeds", "1", "1"], ["--scenarios", "normal", "normal"],
    ["--pnl-quantile", "NaN"], ["--pnl-quantile", "0"], ["--pnl-quantile", "0.6"],
    ["--drawdown-quantile", "0.4"], ["--drawdown-quantile", "2"],
    ["--risk-mode", "operational-limits"], ["--purpose", " "],
    ["--from", "2027-01-05T00:00:00+00:00"],
    ["--dsn-env", "ENSEMBLE_MISSING_DSN"],
    ["--max-parallel", "0"], ["--max-parallel", "-1"],
    ["--max-parallel", "1.5"], ["--max-parallel", "many"],
])
def test_invalid_plan_never_starts_a_trial(monkeypatch, tmp_path, extra):
    calls = fake_research(monkeypatch)
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, tmp_path, extra)
    assert error.value.code == 2
    assert calls == []
    assert not (tmp_path / "path with spaces").exists()


def test_nearest_rank_is_exact_for_small_samples_and_ties():
    values = [Decimal("2.2"), Decimal("-1.1"), Decimal("2.2"), Decimal("0.0")]
    assert ensemble.nearest_rank(values, Decimal("0.05")) == Decimal("-1.1")
    assert ensemble.nearest_rank(values, Decimal("0.5")) == 0
    assert ensemble.nearest_rank(values, Decimal("0.95")) == Decimal("2.2")
    assert ensemble.nearest_rank([Decimal("1.1234567890123456789")], Decimal(1)) == Decimal(
        "1.1234567890123456789"
    )


@pytest.mark.parametrize("broken", ["missing", "malformed", "wrong_type", "outside_directory"])
def test_missing_or_invalid_child_output_is_not_success(monkeypatch, tmp_path, broken):
    calls = []

    def run(command, *, stdout, stderr, env, job):
        calls.append(command)
        if broken == "malformed":
            stdout.write("not json")
        elif broken == "wrong_type":
            stdout.write("[]")
        elif broken == "outside_directory":
            stdout.write(json.dumps({"run_dir": str(tmp_path / "other")}))
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(ensemble, "_start_trial", run)
    with pytest.raises(SystemExit, match="1"):
        invoke(monkeypatch, tmp_path)
    _, results, summary = artifacts(tmp_path)
    assert len(calls) == 6
    assert all(trial["status"] == "failed" for trial in results["trials"])
    assert all(group["distribution"] is None for group in summary["scenarios"].values())


def test_code_changes_while_child_runs_fail_the_trial(monkeypatch, tmp_path):
    def mutate(index, manifest, metrics, out):
        if index == 2:
            monkeypatch.setattr(ensemble, "git_state", lambda: {**REPRO, "git_dirty": True})
    fake_research(monkeypatch, mutate)
    with pytest.raises(SystemExit, match="1"):
        invoke(monkeypatch, tmp_path)
    _, results, summary = artifacts(tmp_path)
    assert results["trials"][0]["status"] == "succeeded"
    assert all(trial["status"] == "failed" for trial in results["trials"][1:])
    assert "git state changed" in results["trials"][1]["error"]
    assert summary["status"] == "incomplete"


def test_missing_fingerprint_and_fabricated_no_fill_pnl_are_rejected(monkeypatch, tmp_path):
    def mutate(index, manifest, metrics, out):
        if index == 1:
            del manifest["swap_dataset_hash"]
        if index == 2:
            metrics.update(fills="0", trades="0", net_pnl="1", realized_pnl="1",
                           final_equity="1000001")
    fake_research(monkeypatch, mutate)
    with pytest.raises(SystemExit, match="1"):
        invoke(monkeypatch, tmp_path)
    _, results, _ = artifacts(tmp_path)
    assert "swap_dataset_hash" in results["trials"][0]["error"]
    assert "no-fill" in results["trials"][1]["error"]


def test_operational_limits_are_recorded_without_changing_the_config(monkeypatch, tmp_path):
    from trading.config import load_config

    config = load_config("backtest")
    config = config.model_copy(update={"risk": config.risk.model_copy(update={
        "daily_loss_halt_pct": Decimal("0.75"),
        "rolling_24h_loss_halt_pct": Decimal("1.00"),
        "high_water_mark_drawdown_halt_pct": Decimal("3.00"),
    })})
    monkeypatch.setattr(ensemble, "load_config", lambda env: config)
    fake_research(monkeypatch)
    invoke(monkeypatch, tmp_path, ["--risk-mode", "operational-limits"])
    plan, _, summary = artifacts(tmp_path)
    assert summary["status"] == "complete"
    assert summary["risk_mode"] == "operational-limits"
    assert summary["loss_halts_pct"]["daily_loss_halt_pct"] == "0.75"
    assert plan["config"] == config.model_dump(mode="json")
