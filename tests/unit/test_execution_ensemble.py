"""実行計画・失敗の保持と、scenario ごとの Decimal 分布。"""
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from trading.backtest import execution_ensemble as ensemble

REPRO = {"git_commit": "a" * 40, "git_dirty": False}


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
    return tuple(json.loads((out / name).read_text()) for name in (
        "plan.json", "results.json", "summary.json",
    ))


def fake_research(monkeypatch, mutate=None):
    calls = []

    def run(command, *, stdout, stderr, check, env):
        assert check is False
        assert env["TRADING_DB_DSN"] == "synthetic-only"
        assert command[:3] == [sys.executable, "-m", "trading.backtest.research"]
        out = Path(command[command.index("--out") + 1])
        plan = json.loads((out.parent / "plan.json").read_text())
        saved = json.loads((out.parent / "results.json").read_text())
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
                return subprocess.CompletedProcess(command, code)
        (run_dir / "manifest.json").write_text(json.dumps(manifest))
        (run_dir / "summary.json").write_text(json.dumps({"symbol": "USDJPY", "metrics": metrics}))
        stdout.write(json.dumps({"run_dir": str(run_dir)}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(ensemble.subprocess, "run", run)
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

    def run(command, *, stdout, stderr, check, env):
        calls.append(command)
        if broken == "malformed":
            stdout.write("not json")
        elif broken == "wrong_type":
            stdout.write("[]")
        elif broken == "outside_directory":
            stdout.write(json.dumps({"run_dir": str(tmp_path / "other")}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(ensemble.subprocess, "run", run)
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
