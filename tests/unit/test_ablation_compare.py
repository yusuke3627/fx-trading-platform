import json
import math
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading.backtest.ablation_compare import (
    DEFERRED_SWAP,
    KEEP,
    REMOVE,
    UNDECIDED_DIFFERENCE,
    UNDECIDED_SAMPLE,
    ArmSummary,
    RunArtifacts,
    arm_summary,
    block_bootstrap_interval,
    block_difference_interval,
    broker_day,
    difference_interval,
    judge,
    load_run,
    main,
    report,
    verify_comparable,
)
from trading.backtest.engine import BacktestResult, TradeRecord
from trading.backtest.policy_event_study import bootstrap_interval
from trading.backtest.report import write_report
from trading.backtest.research import parse_param_value
from trading.backtest.run_coverage import run_coverage
from trading.data.market.clock import broker_label_to_known
from trading.data.market.dukascopy import known_to_broker_label
from trading.strategy.parameters import ParamValue

PERIOD_FROM = datetime(2026, 1, 1, tzinfo=UTC)
PERIOD_TO = datetime(2026, 2, 1, tzinfo=UTC)


def arm(count: int, mean: str, *, unpriced_rollovers: int = 0) -> ArmSummary:
    value = Decimal(mean)
    return ArmSummary(
        count=count,
        total=value * count,
        mean=value,
        hit_rate=0.5,
        max_drawdown=Decimal(10),
        low=float("nan"),
        high=float("nan"),
        block_low=float("nan"),
        block_high=float("nan"),
        blocks=1,
        unpriced_rollovers=unpriced_rollovers,
    )


@pytest.mark.parametrize(
    ("with_", "without", "interval", "expected"),
    [
        (arm(10, "2"), arm(10, "1"), (0.1, 1.5), KEEP),
        (arm(10, "1"), arm(20, "1"), (-0.5, 0.5), REMOVE),
        (arm(5, "1"), arm(5, "2"), (-2.0, 0.0), UNDECIDED_SAMPLE),
        (
            arm(10, "1"),
            arm(10, "1"),
            (-0.5, 0.5),
            UNDECIDED_DIFFERENCE,
        ),
        (
            arm(0, "NaN"),
            arm(10, "1"),
            (float("nan"), float("nan")),
            UNDECIDED_SAMPLE,
        ),
    ],
)
def test_judge_applies_the_pre_registered_rules(
    with_: ArmSummary,
    without: ArmSummary,
    interval: tuple[float, float],
    expected: str,
):
    assert judge(with_, without, interval) == expected


@pytest.mark.parametrize(("with_unpriced", "without_unpriced"), [(1, 0), (0, 15), (1, 15)])
@pytest.mark.parametrize(
    ("with_count", "with_mean", "without_count", "without_mean", "interval"),
    [
        (10, "2", 10, "1", (0.1, 1.5)),
        (10, "1", 20, "1", (-0.5, 0.5)),
        (5, "1", 5, "2", (-2.0, 0.0)),
        (10, "1", 10, "1", (-0.5, 0.5)),
    ],
)
def test_unpriced_swap_defers_every_statistical_verdict(
    with_unpriced, without_unpriced, with_count, with_mean, without_count, without_mean, interval,
):
    with_ = arm(with_count, with_mean, unpriced_rollovers=with_unpriced)
    without = arm(without_count, without_mean, unpriced_rollovers=without_unpriced)

    assert judge(with_, without, interval) == DEFERRED_SWAP


def test_difference_interval_is_seeded_and_independently_resampled():
    with_pnls = [1.0, 2.0, 8.0, 13.0]
    without_pnls = [-3.0, 1.0, 4.0, 5.0]

    first = difference_interval(with_pnls, without_pnls, seed=42)

    assert difference_interval(with_pnls, without_pnls, seed=42) == first
    assert difference_interval(with_pnls, without_pnls, seed=43) != first
    assert difference_interval([10.0, 10.0], [1.0, 1.0], seed=42)[0] > 0


def test_difference_interval_needs_two_trades_in_each_arm():
    low, high = difference_interval([1.0], [1.0, 2.0], seed=42)

    assert math.isnan(low)
    assert math.isnan(high)


def test_broker_day_normalizes_offsets_and_splits_at_broker_midnight():
    assert broker_day(datetime.fromisoformat("2026-01-16T00:30:00+09:00")) == broker_day(
        datetime.fromisoformat("2026-01-15T15:30:00+00:00")
    ) == date(2026, 1, 15)
    midnight = datetime(2026, 1, 16, tzinfo=UTC)
    assert broker_day(midnight - timedelta(microseconds=1)) == date(2026, 1, 15)
    assert broker_day(midnight) == date(2026, 1, 16)


@pytest.mark.parametrize(
    ("known", "expected_day"),
    [
        (datetime(2026, 1, 15, 21, 30, tzinfo=UTC), date(2026, 1, 15)),
        (datetime(2026, 1, 15, 22, 30, tzinfo=UTC), date(2026, 1, 16)),
        (datetime(2026, 7, 15, 20, 30, tzinfo=UTC), date(2026, 7, 15)),
        (datetime(2026, 7, 15, 21, 30, tzinfo=UTC), date(2026, 7, 16)),
    ],
)
def test_broker_day_uses_the_engine_label_without_a_second_conversion(known, expected_day):
    anchor = timedelta(hours=7)
    label = known_to_broker_label(known, anchor)

    assert broker_day(label) == label.date() == expected_day
    if expected_day.day == 16:
        assert broker_day(label) != known.astimezone(UTC).date()
    assert broker_label_to_known(label, anchor) == known


def test_block_bootstrap_preserves_daily_clustering_and_is_seeded():
    pnls = [
        value for value in (-50.0, -31.0, -19.0, -2.0, 13.0, 23.0, 37.0, 80.0)
        for _ in range(10)
    ]
    blocks = [date(2026, 1, day) for day in range(1, 9) for _ in range(10)]

    interval = block_bootstrap_interval(pnls, blocks, seed=42)
    iid_low, iid_high = bootstrap_interval(pnls, seed=42)

    assert interval[0] < iid_low
    assert interval[1] > iid_high
    assert block_bootstrap_interval(pnls, blocks, seed=42) == interval
    assert block_bootstrap_interval(pnls, blocks, seed=43) != interval
    assert block_bootstrap_interval(pnls[::-1], blocks[::-1], seed=42) == interval


def test_block_difference_resamples_the_arms_independently():
    pnls = [-50.0, -31.0, -19.0, -2.0, 13.0, 23.0, 37.0, 80.0]
    blocks = [date(2026, 1, day) for day in range(1, 9)]

    interval = block_difference_interval(pnls, blocks, pnls, blocks, seed=42)

    assert interval[0] < 0 < interval[1]
    assert interval == difference_interval(pnls, pnls, seed=42)
    assert block_difference_interval(pnls, blocks, pnls, blocks, seed=42) == interval
    assert block_difference_interval(pnls, blocks, pnls, blocks, seed=43) != interval
    assert block_difference_interval(
        pnls[::-1], blocks[::-1], pnls[::-1], blocks[::-1], seed=42
    ) == interval


def test_block_intervals_weight_all_trades_in_selected_days():
    pnls = [-10.0] + [1.0] * 3 + [10.0] * 9
    blocks = [date(2026, 1, 1)] + [date(2026, 1, 2)] * 3 + [date(2026, 1, 3)] * 9
    # 両端は日1を2回・日2を1回、および日1を1回・日3を2回抽出した平均。
    expected = ((-10 * 2 + 1 * 3) / (2 + 3), (-10 + 10 * 18) / (1 + 18))

    assert block_bootstrap_interval(pnls, blocks, seed=42) == pytest.approx(expected)
    assert block_difference_interval(
        pnls, blocks, [0.0, 0.0], [date(2026, 1, 1), date(2026, 1, 2)], seed=42
    ) == pytest.approx(expected)


@pytest.mark.parametrize("count", [0, 1, 20])
def test_block_bootstrap_needs_two_days_even_with_many_trades(count):
    low, high = block_bootstrap_interval([1.0] * count, [date(2026, 1, 1)] * count, seed=42)

    assert math.isnan(low)
    assert math.isnan(high)


@pytest.mark.parametrize("count", [0, 1, 20])
@pytest.mark.parametrize("short_arm", ["with", "without"])
def test_block_difference_needs_two_days_in_each_arm(count, short_arm):
    short = ([1.0] * count, [date(2026, 1, 1)] * count)
    full = ([1.0, 2.0], [date(2026, 1, 1), date(2026, 1, 2)])
    with_, without = (short, full) if short_arm == "with" else (full, short)

    low, high = block_difference_interval(*with_, *without, seed=42)

    assert math.isnan(low)
    assert math.isnan(high)


def test_block_intervals_require_one_block_key_per_pnl():
    pnls = [1.0, 2.0]
    blocks = [date(2026, 1, 1), date(2026, 1, 2)]

    with pytest.raises(ValueError):
        block_bootstrap_interval(pnls, blocks[:1], seed=42)
    with pytest.raises(ValueError):
        block_difference_interval(pnls[:1], blocks, pnls, blocks, seed=42)
    with pytest.raises(ValueError):
        block_difference_interval(pnls, blocks, pnls, blocks[:1], seed=42)


@pytest.mark.parametrize("unpriced_rollovers", [0, 1])
def test_arm_summary_keeps_intervals_and_marks_unpriced_swap_as_deferred(unpriced_rollovers):
    pnls = [Decimal("1.1"), Decimal("2.2"), Decimal("3.3")]
    blocks = [date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 2)]

    summary = arm_summary(
        pnls, blocks, Decimal("4.5"), seed=43, unpriced_rollovers=unpriced_rollovers,
    )

    assert (summary.low, summary.high) == bootstrap_interval([float(p) for p in pnls], seed=43)
    assert (summary.block_low, summary.block_high) == block_bootstrap_interval(
        [float(p) for p in pnls], blocks, seed=43
    )
    assert summary.count == 3
    assert summary.blocks == 2
    assert summary.max_drawdown == Decimal("4.5")
    assert summary.unpriced_rollovers == unpriced_rollovers
    assert summary.hold_reason == (DEFERRED_SWAP if unpriced_rollovers else None)


def run_metrics(**overrides: str) -> dict[str, str]:
    return {
        "max_drawdown": "0",
        "carry_total": "0",
        "unpriced_rollovers": "0",
        "trades": "0",
        "open_positions_at_end": "0",
        "pending_commands_at_end": "0",
        **overrides,
    }


def backtest_result(
    pnls: list[Decimal], carries: list[Decimal], max_drawdown: str,
    entry_ats: list[datetime] | None = None,
) -> BacktestResult:
    if entry_ats is None:
        entry_ats = [
            PERIOD_TO - timedelta(hours=len(pnls) - index + 1)
            for index in range(len(pnls))
        ]
    trades = [
        TradeRecord(
            entry_id=f"entry-{index}",
            strategy_id="post_event_failed_breakout",
            symbol="USDJPY",
            entry_at=entry_at,
            exit_at=entry_at + timedelta(hours=1),
            direction="LONG",
            quantity=Decimal(1000),
            entry_price=Decimal(150),
            exit_price=Decimal(150) + pnl / Decimal(1000),
            net_pnl=pnl,
            carry=carry,
            reason="CLOSE",
        )
        for index, (pnl, carry, entry_at) in enumerate(zip(pnls, carries, entry_ats, strict=True))
    ]
    return BacktestResult(
        symbol="USDJPY",
        fills=[],
        trades=trades,
        equity_curve=[],
        snapshots=[],
        risk_rejections=[],
        rejected_commands=0,
        metrics=run_metrics(
            max_drawdown=max_drawdown,
            carry_total=str(sum(carries, Decimal(0))),
            trades=str(len(pnls)),
        ),
    )


def manifest(
    run_id: str,
    param_overrides: dict,
    *,
    resolved_enabled: ParamValue | None = None,
    param: str = "macro_confirmation_enabled",
    strategy_id: str = "post_event_failed_breakout",
    period_from: datetime = PERIOD_FROM,
    period_to: datetime = PERIOD_TO,
) -> dict:
    if resolved_enabled is None:
        resolved_enabled = param_overrides.get(param, True)
    return {
        "run_id": run_id,
        "git_commit": "0123456789abcdef",
        "git_dirty": False,
        "python_version": "3.12.4",
        "environment": "backtest",
        "symbol": "USDJPY",
        "strategy_id": strategy_id,
        "strategy_version": "0.2.0",
        "engine_version": "0.5.0",
        "scenario": "normal",
        "seed": 42,
        "tick_count": 1000,
        "dataset_hash": "dataset-test-hash",
        "feature_dataset_hash": "feature-test-hash",
        "swap_dataset_hash": "swap-test-hash",
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "warmup_days": 2.0,
        "broker_server_ahead_of_ny_hours": 7.0,
        "param_overrides": param_overrides,
        "resolved_parameters": {
            param: resolved_enabled,
        },
    }


def test_load_run_and_report_round_trip_trade_pnls_and_provenance(tmp_path):
    with_pnls = [Decimal("12.5"), Decimal("-2.5")]
    with_carries = [Decimal("-1.5"), Decimal(0)]
    without_pnls = [Decimal("1.0"), Decimal("2.0"), Decimal("3.0")]
    period_to = datetime(2026, 7, 1, tzinfo=UTC)
    with_entries = [
        datetime(2026, 1, 31, 23, 30, tzinfo=UTC),
        datetime(2026, 6, 30, 12, tzinfo=UTC),
    ]
    without_entries = [
        datetime(2026, 1, 15, tzinfo=UTC),
        datetime(2026, 3, 15, tzinfo=UTC),
        datetime(2026, 6, 30, 12, tzinfo=UTC),
    ]
    with_dir = write_report(
        backtest_result(with_pnls, with_carries, "4.5", entry_ats=with_entries),
        manifest("with-run", {}, period_to=period_to),
        tmp_path,
    )
    without_dir = write_report(
        backtest_result(without_pnls, [Decimal(0)] * 3, "6.5", entry_ats=without_entries),
        manifest("without-run", {"macro_confirmation_enabled": False}, period_to=period_to),
        tmp_path,
    )

    with_run = load_run(with_dir)
    without_run = load_run(without_dir)
    rendered = report(with_run, without_run, "macro_confirmation_enabled", seed=42)

    assert with_run.pnls == [Decimal("11.0"), Decimal("-2.5")]
    assert without_run.pnls == without_pnls
    assert 'param_overrides        {"macro_confirmation_enabled": false}' in rendered
    assert "with-run" in rendered
    assert "without-run" in rendered
    assert f"{'carry_total':<28} {'-1.5':>22} {'0':>22}" in rendered
    assert f"{'unpriced_rollovers':<28} {'0':>22} {'0':>22}" in rendered
    assert "verdict:" in rendered
    assert f"{'blocks':<28} {2:>22} {3:>22}" in rendered
    assert f"{'expectancy_ci90_block':<28} {'[-2.5, 11]':>22} {'[1.33333, 2.66667]':>22}" in rendered
    assert "difference of means (with - without) block CI90 [-4.83333, 9.33333] seed=42" in rendered
    lines = rendered.splitlines()
    mean_index = next(index for index, line in enumerate(lines) if line.startswith("mean CI90"))
    assert lines[mean_index + 1].startswith("blocks ")
    assert lines[mean_index + 2].startswith("expectancy_ci90_block ")
    assert lines[-3].startswith("difference of means (with - without): ")
    assert lines[-2].startswith("difference of means (with - without) block CI90 ")
    assert lines[-1].startswith("verdict: ")
    assert b"\r\n" not in (with_dir / "trades.csv").read_bytes()
    for label, run, run_dir, entries, empty_months in (
        ("with", with_run, with_dir, with_entries, ["2026-02", "2026-03", "2026-04", "2026-05"]),
        ("without", without_run, without_dir, without_entries, ["2026-02", "2026-04", "2026-05"]),
    ):
        assert run.entry_ats == entries
        coverage = json.loads((run_dir / "summary.json").read_text())["coverage"]
        assert coverage == {
            "first_trade_at": entries[0].isoformat(),
            "last_trade_at": entries[-1].isoformat(),
            "months_with_trades": len(entries),
            "months_in_period": 6,
            "empty_months": empty_months,
            "trailing_blackout_days": 0.5,
        }
        section = rendered.split(f"{label}:\n", 1)[1].split("\nwithout:\n")[0]
        for field in ("first_trade_at", "last_trade_at"):
            assert f"  {field:<22} {coverage[field]}" in section
        assert f"  {'months_with_trades':<22} {coverage['months_with_trades']}/6" in section


@pytest.mark.parametrize(
    ("with_unpriced", "without_unpriced"), [(0, 0), (1, 0), (0, 15), (1, 15)],
)
def test_main_defers_only_the_unpriced_arms_and_their_comparison(
    tmp_path, monkeypatch, capsys, with_unpriced, without_unpriced,
):
    runs = {}
    for label, pnl, unpriced in (
        ("with", Decimal(2), with_unpriced), ("without", Decimal(1), without_unpriced),
    ):
        result = backtest_result([pnl] * 10, [Decimal(0)] * 10, "0")
        result.metrics["unpriced_rollovers"] = str(unpriced)
        overrides = {} if label == "with" else {"macro_confirmation_enabled": False}
        runs[label] = write_report(result, manifest(label, overrides), tmp_path)
    monkeypatch.setattr(
        sys, "argv",
        [
            "ablation_compare", "--with", str(runs["with"]), "--without", str(runs["without"]),
            "--param", "macro_confirmation_enabled", "--seed", "42",
        ],
    )

    main()

    rendered = capsys.readouterr().out
    expected = DEFERRED_SWAP if with_unpriced or without_unpriced else KEEP
    assert rendered.splitlines()[-1] == f"verdict: {expected}"
    assert f"{'unpriced_rollovers':<28} {with_unpriced:>22} {without_unpriced:>22}" in rendered
    assert "difference of means (with - without): 1 CI90 [1, 1] seed=42" in rendered
    for label, unpriced in (("with", with_unpriced), ("without", without_unpriced)):
        section = rendered.split(f"{label}:\n", 1)[1].split("\nwithout:\n")[0].split("\n\n")[0]
        assert ("hold_reason" in section) == bool(unpriced)
        if unpriced:
            assert DEFERRED_SWAP in section
            assert "この腕の損益・区間は参考値" in section


def test_load_run_reports_a_missing_trades_file(tmp_path):
    run_dir = write_report(
        backtest_result([], [], "0"), manifest("missing-trades", {}), tmp_path
    )
    (run_dir / "trades.csv").unlink()

    with pytest.raises(SystemExit, match="trades.csv"):
        load_run(run_dir)


def test_load_run_rejects_trade_count_mismatch(tmp_path):
    run_dir = write_report(
        backtest_result([Decimal(1), Decimal(2)], [Decimal(0)] * 2, "0"),
        manifest("truncated-trades", {}),
        tmp_path,
    )
    trades_path = run_dir / "trades.csv"
    rows = trades_path.read_text(encoding="utf-8").splitlines(keepends=True)
    trades_path.write_text("".join(rows[:-1]), encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        load_run(run_dir)

    assert str(trades_path) in str(error.value)
    assert "actual rows=1, summary metrics.trades=2" in str(error.value)


def test_verify_comparable_rejects_dataset_mismatch():
    with_manifest = manifest("with-run", {})
    without_manifest = manifest(
        "without-run", {"macro_confirmation_enabled": False}
    )
    without_manifest["dataset_hash"] = "different-dataset-hash"

    with pytest.raises(SystemExit, match="dataset_hash"):
        verify_comparable(
            RunArtifacts(
                with_manifest,
                run_metrics(),
                [],
                entry_ats=[],
            ),
            RunArtifacts(
                without_manifest,
                run_metrics(),
                [],
                entry_ats=[],
            ),
            "macro_confirmation_enabled",
        )


def test_verify_comparable_rejects_runs_without_a_known_commit():
    with_manifest = manifest("with-run", {})
    without_manifest = manifest(
        "without-run", {"macro_confirmation_enabled": False}
    )
    with_manifest["git_commit"] = "unknown"
    without_manifest["git_commit"] = "unknown"

    with pytest.raises(SystemExit, match="git_commit='unknown'"):
        verify_comparable(
            RunArtifacts(with_manifest, run_metrics(), [], entry_ats=[]),
            RunArtifacts(without_manifest, run_metrics(), [], entry_ats=[]),
            "macro_confirmation_enabled",
        )


def test_verify_comparable_rejects_python_version_mismatch():
    with_manifest = manifest("with-run", {})
    without_manifest = manifest(
        "without-run", {"macro_confirmation_enabled": False}
    )
    without_manifest["python_version"] = "3.13.1"

    with pytest.raises(SystemExit, match="python_version"):
        verify_comparable(
            RunArtifacts(with_manifest, run_metrics(), [], entry_ats=[]),
            RunArtifacts(without_manifest, run_metrics(), [], entry_ats=[]),
            "macro_confirmation_enabled",
        )


def test_verify_comparable_requires_disabled_without_arm():
    with pytest.raises(SystemExit, match="macro_confirmation_enabled"):
        verify_comparable(
            RunArtifacts(
                manifest("with-run", {}),
                run_metrics(),
                [],
                entry_ats=[],
            ),
            RunArtifacts(
                manifest("without-run", {}),
                run_metrics(),
                [],
                entry_ats=[],
            ),
            "macro_confirmation_enabled",
        )


def test_verify_comparable_rejects_instrument_override_of_without_arm():
    without_manifest = manifest(
        "without-run",
        {"macro_confirmation_enabled": False},
        resolved_enabled=True,
    )

    with pytest.raises(SystemExit, match="resolved_parameters"):
        verify_comparable(
            RunArtifacts(
                manifest("with-run", {}),
                run_metrics(),
                [],
                entry_ats=[],
            ),
            RunArtifacts(
                without_manifest,
                run_metrics(),
                [],
                entry_ats=[],
            ),
            "macro_confirmation_enabled",
        )


def test_verify_comparable_rejects_other_override_mismatch_without_mutation():
    without_overrides = {"macro_confirmation_enabled": False, "atr_period": 20}
    without_manifest = manifest("without-run", without_overrides)

    with pytest.raises(SystemExit, match="non-ablation param_overrides"):
        verify_comparable(
            RunArtifacts(
                manifest("with-run", {}),
                run_metrics(),
                [],
                entry_ats=[],
            ),
            RunArtifacts(
                without_manifest,
                run_metrics(),
                [],
                entry_ats=[],
            ),
            "macro_confirmation_enabled",
        )

    assert without_manifest["param_overrides"] == without_overrides


def test_verify_comparable_rejects_strategy_id_mismatch():
    with_manifest = manifest("with-run", {})
    without_manifest = manifest(
        "without-run", {"macro_confirmation_enabled": False}
    )
    without_manifest["strategy_id"] = "failed_spike_reversal"

    with pytest.raises(SystemExit, match="strategy_id"):
        verify_comparable(
            RunArtifacts(
                with_manifest,
                run_metrics(),
                [],
                entry_ats=[],
            ),
            RunArtifacts(
                without_manifest,
                run_metrics(),
                [],
                entry_ats=[],
            ),
            "macro_confirmation_enabled",
        )


def test_verify_comparable_rejects_an_open_position_at_period_end():
    with pytest.raises(SystemExit, match="open_positions_at_end"):
        verify_comparable(
            RunArtifacts(
                manifest("with-run", {}),
                run_metrics(),
                [],
                entry_ats=[],
            ),
            RunArtifacts(
                manifest("without-run", {"macro_confirmation_enabled": False}),
                run_metrics(open_positions_at_end="1"),
                [],
                entry_ats=[],
            ),
            "macro_confirmation_enabled",
        )


def test_verify_comparable_rejects_a_command_in_flight_at_period_end():
    with pytest.raises(SystemExit, match="pending_commands_at_end"):
        verify_comparable(
            RunArtifacts(
                manifest("with-run", {}),
                run_metrics(),
                [],
                entry_ats=[],
            ),
            RunArtifacts(
                manifest("without-run", {"macro_confirmation_enabled": False}),
                run_metrics(pending_commands_at_end="1"),
                [],
                entry_ats=[],
            ),
            "macro_confirmation_enabled",
        )


def test_partial_closes_are_one_bootstrap_sample(tmp_path):
    from dataclasses import replace

    result = backtest_result(
        [Decimal(10), Decimal(-5), Decimal(20)],
        [Decimal(-1), Decimal(-2), Decimal(-3)], "0",
    )
    result.trades[1] = replace(
        result.trades[1],
        entry_id=result.trades[0].entry_id,
        entry_at=result.trades[0].entry_at,
    )
    run_dir = write_report(result, manifest("partial", {}), tmp_path)
    run = load_run(run_dir)
    assert run.pnls == [Decimal(2), Decimal(17)]
    blocks = [broker_day(at) for at in run.entry_ats]
    summary = arm_summary(
        run.pnls, blocks, Decimal(0), seed=42,
        unpriced_rollovers=int(run.metrics["unpriced_rollovers"]),
    )
    assert summary.count == 2
    assert summary.blocks == 1
    assert run.entry_ats == [result.trades[0].entry_at, result.trades[2].entry_at]
    coverage = run_coverage(run.entry_ats, PERIOD_FROM, PERIOD_TO)
    assert coverage == run_coverage(
        (result.trades[index].entry_at for index in (0, 2)), PERIOD_FROM, PERIOD_TO
    )
    assert coverage.first_trade_at == result.trades[0].entry_at
    assert coverage.last_trade_at == result.trades[2].entry_at
    assert coverage.months_with_trades == 1


def test_legacy_trade_csv_requires_new_replay(tmp_path):
    run_dir = write_report(backtest_result([], [], "0"), manifest("old", {}), tmp_path)
    (run_dir / "trades.csv").write_text("net_pnl,carry\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="no entry_id"):
        load_run(run_dir)


def test_verify_comparable_accepts_another_strategy_and_parameter():
    with_manifest = manifest(
        "with-run", {"horizon_exit_enabled": True},
        resolved_enabled=True,
        param="horizon_exit_enabled",
        strategy_id="failed_spike_reversal",
    )
    without_manifest = manifest(
        "without-run", {"horizon_exit_enabled": False},
        resolved_enabled=False,
        param="horizon_exit_enabled",
        strategy_id="failed_spike_reversal",
    )

    verify_comparable(
        RunArtifacts(with_manifest, run_metrics(), [], entry_ats=[]),
        RunArtifacts(without_manifest, run_metrics(), [], entry_ats=[]),
        "horizon_exit_enabled",
    )


def test_verify_comparable_requires_disabled_without_arm_for_another_parameter():
    with_manifest = manifest(
        "with-run", {"horizon_exit_enabled": True},
        resolved_enabled=True,
        param="horizon_exit_enabled",
        strategy_id="failed_spike_reversal",
    )
    without_manifest = manifest(
        "without-run", {},
        resolved_enabled=False,
        param="horizon_exit_enabled",
        strategy_id="failed_spike_reversal",
    )

    with pytest.raises(SystemExit, match="param_overrides.horizon_exit_enabled"):
        verify_comparable(
            RunArtifacts(with_manifest, run_metrics(), [], entry_ats=[]),
            RunArtifacts(without_manifest, run_metrics(), [], entry_ats=[]),
            "horizon_exit_enabled",
        )


def test_verify_comparable_rejects_missing_resolved_parameter_in_with_arm():
    with_manifest = manifest(
        "with-run", {"horizon_exit_enabled": True},
        resolved_enabled=True,
        param="horizon_exit_enabled",
        strategy_id="failed_spike_reversal",
    )
    without_manifest = manifest(
        "without-run", {"horizon_exit_enabled": False},
        resolved_enabled=False,
        param="horizon_exit_enabled",
        strategy_id="failed_spike_reversal",
    )
    del with_manifest["resolved_parameters"]["horizon_exit_enabled"]

    with pytest.raises(SystemExit, match="resolved_parameters.horizon_exit_enabled"):
        verify_comparable(
            RunArtifacts(with_manifest, run_metrics(), [], entry_ats=[]),
            RunArtifacts(without_manifest, run_metrics(), [], entry_ats=[]),
            "horizon_exit_enabled",
        )


def test_main_requires_param(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["ablation_compare", "--with", "with-run", "--without", "without-run"]
    )

    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 2
    assert "--param" in capsys.readouterr().err


def test_main_compares_another_parameter_with_requested_seed(tmp_path, monkeypatch, capsys):
    with_dir = write_report(
        backtest_result([Decimal(3), Decimal(4)], [Decimal(0)] * 2, "0"),
        manifest(
            "with-run", {"horizon_exit_enabled": True},
            resolved_enabled=True,
            param="horizon_exit_enabled",
            strategy_id="failed_spike_reversal",
        ),
        tmp_path,
    )
    without_dir = write_report(
        backtest_result([Decimal(1), Decimal(2)], [Decimal(0)] * 2, "0"),
        manifest(
            "without-run", {"horizon_exit_enabled": False},
            resolved_enabled=False,
            param="horizon_exit_enabled",
            strategy_id="failed_spike_reversal",
        ),
        tmp_path,
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "ablation_compare", "--with", str(with_dir), "--without", str(without_dir),
            "--param", "horizon_exit_enabled", "--seed", "43",
        ],
    )

    main()

    rendered = capsys.readouterr().out
    assert "seed=43" in rendered
    assert "verdict:" in rendered


@pytest.mark.parametrize(
    ("entry_ats", "empty_months", "blackout_days"),
    [
        (
            [],
            ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"],
            None,
        ),
        (
            [datetime(2026, month, 15, tzinfo=UTC) for month in range(1, 7)],
            [], 16.0,
        ),
        (
            [datetime(2026, 2, 15, tzinfo=UTC), datetime(2026, 1, 15, tzinfo=UTC)],
            ["2026-03", "2026-04", "2026-05", "2026-06"], 136.0,
        ),
    ],
    ids=["no-trades", "every-month", "trailing-gap"],
)
def test_write_report_serializes_coverage(tmp_path, entry_ats, empty_months, blackout_days):
    count = len(entry_ats)
    result = backtest_result([Decimal(1)] * count, [Decimal(0)] * count, "0", entry_ats)
    run_dir = write_report(
        result, manifest("coverage", {}, period_to=datetime(2026, 7, 1, tzinfo=UTC)), tmp_path
    )

    summary = json.loads((run_dir / "summary.json").read_text())

    assert summary["coverage"] == {
        "first_trade_at": min(entry_ats).isoformat() if entry_ats else None,
        "last_trade_at": max(entry_ats).isoformat() if entry_ats else None,
        "months_with_trades": count,
        "months_in_period": 6,
        "empty_months": empty_months,
        "trailing_blackout_days": blackout_days,
    }
    assert summary["symbol"] == result.symbol
    assert summary["metrics"] == result.metrics
    assert summary["risk_rejections"] == []


def test_write_report_omits_coverage_without_a_period(tmp_path):
    run_manifest = manifest("scripted", {})
    del run_manifest["period_from"]
    del run_manifest["period_to"]
    result = backtest_result([], [], "0")

    run_dir = write_report(result, run_manifest, tmp_path)

    assert json.loads((run_dir / "summary.json").read_text()) == {
        "symbol": result.symbol, "metrics": result.metrics, "risk_rejections": [],
    }


@pytest.mark.parametrize("rejected_arm", ["with", "without"])
def test_verify_comparable_rejects_trailing_gap_with_both_arm_details(rejected_arm):
    period_to = datetime(2026, 7, 1, tzinfo=UTC)
    early_entries = [datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 2, 15, tzinfo=UTC)]
    full_entries = [datetime(2026, 1, 20, tzinfo=UTC), datetime(2026, 6, 30, 12, tzinfo=UTC)]
    runs = {}
    for label in ("with", "without"):
        runs[label] = RunArtifacts(
            manifest(
                label, {} if label == "with" else {"macro_confirmation_enabled": False},
                period_to=period_to,
            ),
            run_metrics(trades="2"), [Decimal(1), Decimal(2)],
            early_entries if label == rejected_arm else full_entries,
        )

    with pytest.raises(SystemExit) as error:
        verify_comparable(runs["with"], runs["without"], "macro_confirmation_enabled")

    message = str(error.value)
    assert "runs are not comparable" in message
    assert f"{rejected_arm} last_trade_at={early_entries[-1].isoformat()}" in message
    assert "136.0 days before period_to=2026-07-01T00:00:00+00:00" in message
    assert "75.1% of the period, limit 10%" in message
    for label, run in runs.items():
        assert (
            f"{label} first_trade_at={run.entry_ats[0].isoformat()} "
            f"last_trade_at={run.entry_ats[-1].isoformat()} empty_months=4/6"
        ) in message


def test_verify_comparable_accepts_exactly_ten_percent_but_rejects_one_second_more():
    from dataclasses import replace

    period_to = datetime(2026, 1, 11, tzinfo=UTC)
    last_at = datetime(2026, 1, 10, tzinfo=UTC)
    with_run = RunArtifacts(
        manifest("with", {}, period_to=period_to), run_metrics(trades="1"),
        [Decimal(1)], [last_at],
    )
    without_run = RunArtifacts(
        manifest("without", {"macro_confirmation_enabled": False}, period_to=period_to),
        run_metrics(trades="1"), [Decimal(1)], [last_at],
    )

    verify_comparable(with_run, without_run, "macro_confirmation_enabled")

    with pytest.raises(SystemExit, match="trailing gap exceeds the comparison limit"):
        verify_comparable(
            replace(with_run, entry_ats=[last_at - timedelta(seconds=1)]),
            without_run, "macro_confirmation_enabled",
        )


def test_report_accepts_different_months_with_trades():
    period_to = datetime(2026, 7, 1, tzinfo=UTC)
    last_at = datetime(2026, 6, 30, 12, tzinfo=UTC)
    with_run = RunArtifacts(
        manifest("with", {}, period_to=period_to), run_metrics(trades="3"),
        [Decimal(1)] * 3,
        [datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 2, 15, tzinfo=UTC), last_at],
    )
    without_run = RunArtifacts(
        manifest("without", {"macro_confirmation_enabled": False}, period_to=period_to),
        run_metrics(trades="3"), [Decimal(1)] * 3,
        [datetime(2026, 1, 20, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC), last_at],
    )

    verify_comparable(with_run, without_run, "macro_confirmation_enabled")
    rendered = report(with_run, without_run, "macro_confirmation_enabled", seed=42)

    for label, run in (("with", with_run), ("without", without_run)):
        section = rendered.split(f"{label}:\n", 1)[1].split("\nwithout:\n")[0]
        assert f"  {'first_trade_at':<22} {run.entry_ats[0].isoformat()}" in section
        assert f"  {'last_trade_at':<22} {last_at.isoformat()}" in section
        assert f"  {'months_with_trades':<22} 3/6" in section


@pytest.mark.parametrize("empty_arms", [("with",), ("without",), ("with", "without")])
def test_report_preserves_sample_verdict_for_empty_runs(tmp_path, empty_arms):
    runs = {}
    for label in ("with", "without"):
        count = 0 if label in empty_arms else 2
        run_dir = write_report(
            backtest_result([Decimal(1)] * count, [Decimal(0)] * count, "0"),
            manifest(label, {} if label == "with" else {"macro_confirmation_enabled": False}),
            tmp_path,
        )
        runs[label] = load_run(run_dir)

    rendered = report(runs["with"], runs["without"], "macro_confirmation_enabled", seed=42)

    for label in empty_arms:
        assert runs[label].entry_ats == []
        section = rendered.split(f"{label}:\n", 1)[1].split("\nwithout:\n")[0]
        assert f"  {'first_trade_at':<22} None" in section
        assert f"  {'last_trade_at':<22} None" in section
        assert f"  {'months_with_trades':<22} 0/1" in section
    assert f"verdict: {UNDECIDED_SAMPLE}" in rendered


@pytest.mark.parametrize("empty_arm", ["with", "without"])
def test_empty_arm_does_not_hide_other_arms_trailing_gap(tmp_path, empty_arm):
    runs = {}
    for label in ("with", "without"):
        count = 0 if label == empty_arm else 1
        run_dir = write_report(
            backtest_result([Decimal(1)] * count, [Decimal(0)] * count, "0", [PERIOD_FROM] * count),
            manifest(label, {} if label == "with" else {"macro_confirmation_enabled": False}),
            tmp_path,
        )
        runs[label] = load_run(run_dir)

    with pytest.raises(SystemExit) as error:
        report(runs["with"], runs["without"], "macro_confirmation_enabled", seed=42)

    message = str(error.value)
    assert "trailing gap exceeds the comparison limit" in message
    assert f"{empty_arm} first_trade_at=None last_trade_at=None empty_months=1/1" in message


def test_legacy_summary_uses_csv_coverage_for_display_and_rejection(tmp_path):
    runs = {}
    for label, entry_ats in (
        ("with", [PERIOD_TO - timedelta(hours=2)]),
        ("without", [PERIOD_TO - timedelta(hours=3)]),
        ("early", [PERIOD_FROM]),
    ):
        run_dir = write_report(
            backtest_result([Decimal(1)], [Decimal(0)], "0", entry_ats),
            manifest(label, {"macro_confirmation_enabled": False} if label == "without" else {}),
            tmp_path,
        )
        summary_path = run_dir / "summary.json"
        summary = json.loads(summary_path.read_text())
        del summary["coverage"]
        summary_path.write_text(json.dumps(summary))
        runs[label] = load_run(run_dir)

    rendered = report(runs["with"], runs["without"], "macro_confirmation_enabled", seed=42)

    assert f"  {'first_trade_at':<22} {runs['with'].entry_ats[0].isoformat()}" in rendered
    assert f"  {'last_trade_at':<22} {runs['without'].entry_ats[0].isoformat()}" in rendered
    assert rendered.count(f"  {'months_with_trades':<22} 1/1") == 2
    with pytest.raises(SystemExit, match="trailing gap exceeds the comparison limit"):
        report(runs["early"], runs["without"], "macro_confirmation_enabled", seed=42)


def test_load_run_rejects_missing_entry_at_in_an_empty_csv(tmp_path):
    run_dir = write_report(backtest_result([], [], "0"), manifest("no-entry-time", {}), tmp_path)
    (run_dir / "trades.csv").write_text("entry_id,net_pnl,carry\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="trades.csv has no entry_at"):
        load_run(run_dir)


@pytest.mark.parametrize("missing_field", ["period_from", "period_to"])
@pytest.mark.parametrize("missing_arm", ["with", "without", "both"])
def test_verify_comparable_requires_both_period_fields(missing_field, missing_arm):
    runs = {}
    for label in ("with", "without"):
        run_manifest = manifest(
            label, {} if label == "with" else {"macro_confirmation_enabled": False}
        )
        if missing_arm in (label, "both"):
            del run_manifest[missing_field]
        runs[label] = RunArtifacts(run_manifest, run_metrics(), [], entry_ats=[])

    with pytest.raises(SystemExit) as error:
        verify_comparable(runs["with"], runs["without"], "macro_confirmation_enabled")

    for label in ("with", "without"):
        if missing_arm in (label, "both"):
            assert f"{label} manifest has no period_from/period_to" in str(error.value)


@pytest.mark.parametrize(
    ("entry_at", "accepted"),
    [
        (PERIOD_FROM, True),
        (PERIOD_FROM - timedelta(seconds=1), False),
        (PERIOD_TO, False),
        (PERIOD_TO + timedelta(seconds=1), False),
    ],
    ids=["inclusive-start", "before-start", "exclusive-end", "after-end"],
)
@pytest.mark.parametrize("checked_arm", ["with", "without"])
def test_verify_comparable_checks_half_open_entry_period(entry_at, accepted, checked_arm):
    runs = {}
    for label in ("with", "without"):
        entries = [PERIOD_TO - timedelta(hours=2)]
        if label == checked_arm:
            entries = [entry_at, *entries]
        runs[label] = RunArtifacts(
            manifest(label, {} if label == "with" else {"macro_confirmation_enabled": False}),
            run_metrics(trades=str(len(entries))), [Decimal(1)] * len(entries), entries,
        )

    if accepted:
        verify_comparable(runs["with"], runs["without"], "macro_confirmation_enabled")
    else:
        with pytest.raises(SystemExit) as error:
            verify_comparable(runs["with"], runs["without"], "macro_confirmation_enabled")
        message = str(error.value)
        assert f"{checked_arm} has 1 entry_at outside" in message
        assert f"first={entry_at.isoformat()}" in message
        assert "trailing gap" not in message


def test_missing_period_and_other_arms_trailing_gap_report_both_reasons():
    with_manifest = manifest("with", {})
    del with_manifest["period_from"]
    with_run = RunArtifacts(with_manifest, run_metrics(), [], entry_ats=[])
    without_run = RunArtifacts(
        manifest("without", {"macro_confirmation_enabled": False}),
        run_metrics(trades="1"), [Decimal(1)], [PERIOD_FROM],
    )

    with pytest.raises(SystemExit) as error:
        verify_comparable(with_run, without_run, "macro_confirmation_enabled")

    message = str(error.value)
    assert "runs are not comparable" in message
    assert "with manifest has no period_from/period_to" in message
    assert "without last_trade_at=2026-01-01T00:00:00+00:00 is 31.0 days" in message
    assert "trailing gap exceeds the comparison limit" in message
    assert "with coverage unavailable" in message
    assert (
        "without first_trade_at=2026-01-01T00:00:00+00:00 "
        "last_trade_at=2026-01-01T00:00:00+00:00 empty_months=0/1"
    ) in message


@pytest.mark.parametrize("explicit_with_override", [False, True])
@pytest.mark.parametrize(
    ("with_value", "without_value"),
    [(0.2, 1.0), (3, 4), ("abc", "xyz"), (True, 1), (1, 1.0), (1, "1")],
)
def test_verify_comparable_accepts_typed_parameter_values(
    with_value, without_value, explicit_with_override,
):
    param = "entry_band_fraction"
    with_manifest = manifest(
        "with-run", {param: with_value} if explicit_with_override else {},
        resolved_enabled=with_value, param=param,
    )
    without_manifest = manifest("without-run", {param: without_value}, param=param)

    verify_comparable(
        RunArtifacts(with_manifest, run_metrics(), [], entry_ats=[]),
        RunArtifacts(without_manifest, run_metrics(), [], entry_ats=[]),
        param, with_value=with_value, without_value=without_value,
    )


@pytest.mark.parametrize(("rejected_arm", "actual"), [("with", 1), ("without", 0)])
def test_verify_comparable_rejects_integers_for_default_boolean_expectations(rejected_arm, actual):
    param = "macro_confirmation_enabled"
    runs = {}
    for label, expected in (("with", True), ("without", False)):
        runs[label] = RunArtifacts(
            manifest(
                label, {param: expected},
                resolved_enabled=actual if label == rejected_arm else expected,
            ),
            run_metrics(), [], entry_ats=[],
        )

    with pytest.raises(SystemExit) as error:
        verify_comparable(runs["with"], runs["without"], param)

    expected = rejected_arm == "with"
    assert (
        f"{rejected_arm} resolved_parameters.{param} must be {expected!r}, got {actual!r}"
    ) in str(error.value)


@pytest.mark.parametrize(
    ("with_value", "without_value", "rejected_arm", "actual"),
    [
        (0.2, 1.0, "without", 1),
        (0.2, 1.0, "without", 0.5),
        (0.2, 1.0, "with", 0.5),
        (3, 1, "without", True),
        (3, 1, "without", 1.0),
        ("abc", "1", "without", 1),
    ],
)
def test_verify_comparable_rejects_resolved_value_mismatch(
    with_value, without_value, rejected_arm, actual,
):
    param = "entry_band_fraction"
    runs = {}
    for label, expected in (("with", with_value), ("without", without_value)):
        runs[label] = RunArtifacts(
            manifest(
                label, {param: expected}, param=param,
                resolved_enabled=actual if label == rejected_arm else expected,
            ),
            run_metrics(), [], entry_ats=[],
        )

    with pytest.raises(SystemExit) as error:
        verify_comparable(
            runs["with"], runs["without"], param,
            with_value=with_value, without_value=without_value,
        )

    expected = with_value if rejected_arm == "with" else without_value
    assert (
        f"{rejected_arm} resolved_parameters.{param} must be {expected!r}, got {actual!r}"
    ) in str(error.value)


@pytest.mark.parametrize("value", [0.2, True, False, 3, "abc"])
def test_verify_comparable_rejects_identical_expected_values(value):
    param = "entry_band_fraction"
    with_run = RunArtifacts(
        manifest("with-run", {}, resolved_enabled=value, param=param),
        run_metrics(), [], entry_ats=[],
    )
    without_run = RunArtifacts(
        manifest("without-run", {param: value}, param=param),
        run_metrics(), [], entry_ats=[],
    )

    with pytest.raises(SystemExit) as error:
        verify_comparable(with_run, without_run, param, with_value=value, without_value=value)

    message = str(error.value)
    assert param in message
    assert f"with={value!r}, without={value!r}" in message


@pytest.mark.parametrize(
    ("with_value", "without_value", "rejected_arm", "actual"),
    [
        (0.2, 1.0, "without", 0.5),
        (0.2, 1.0, "without", 1),
        (0.2, 1.0, "without", None),
        (0.2, 1.0, "with", 0.5),
        (1.0, 0.2, "with", 1),
        (True, False, "with", 1),
        (True, False, "without", 0),
        (3, 1, "without", True),
        ("abc", "1", "without", 1),
    ],
)
def test_verify_comparable_rejects_override_value_mismatch(
    with_value, without_value, rejected_arm, actual,
):
    param = "entry_band_fraction"
    runs = {}
    for label, expected in (("with", with_value), ("without", without_value)):
        overrides = {param: expected}
        if label == rejected_arm:
            overrides = {} if actual is None else {param: actual}
        runs[label] = RunArtifacts(
            manifest(label, overrides, resolved_enabled=expected, param=param),
            run_metrics(), [], entry_ats=[],
        )

    with pytest.raises(SystemExit) as error:
        verify_comparable(
            runs["with"], runs["without"], param,
            with_value=with_value, without_value=without_value,
        )

    expected = with_value if rejected_arm == "with" else without_value
    requirement = "omitted or " if rejected_arm == "with" else ""
    assert (
        f"{rejected_arm} param_overrides.{param} must be {requirement}{expected!r}"
    ) in str(error.value)
    assert "resolved_parameters" not in str(error.value)


@pytest.mark.parametrize(("with_value", "without_value"), [(0.2, 1.0), (1, "1")])
def test_report_prefixes_parameter_values_and_preserves_result_lines(
    tmp_path, with_value, without_value,
):
    param = "entry_band_fraction"
    runs = {}
    for label, value in (("with", with_value), ("without", without_value)):
        run_dir = write_report(
            backtest_result([Decimal(1), Decimal(2)], [Decimal(0)] * 2, "0"),
            manifest(
                label, {} if label == "with" else {param: value},
                resolved_enabled=value, param=param,
            ),
            tmp_path,
        )
        runs[label] = load_run(run_dir)

    rendered = report(
        runs["with"], runs["without"], param, seed=42,
        with_value=with_value, without_value=without_value,
    )

    lines = rendered.splitlines()
    assert lines[0] == f"比較: {param} with={with_value!r} → without={without_value!r}"
    assert lines[1] == "with:"
    assert "without:" in lines
    assert f"{'metric':<28} {'with':>22} {'without':>22}" in lines
    assert f"{'net_pnl_total':<28} {'3':>22} {'3':>22}" in lines
    assert lines[-3] == "difference of means (with - without): 0.0 CI90 [-1, 1] seed=42"
    assert lines[-2] == "difference of means (with - without) block CI90 [nan, nan] seed=42"
    assert lines[-1] == f"verdict: {UNDECIDED_SAMPLE}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.2", 0.2),
        ("1.0", 1.0),
        ("true", True),
        ("TRUE", True),
        ("False", False),
        ("3", 3),
        ("+3", 3),
        ("-3", -3),
        ("1e2", 100.0),
        ("abc", "abc"),
    ],
)
def test_parse_param_value_preserves_scalar_types(text, expected):
    parsed = parse_param_value(text)

    assert parsed == expected
    assert type(parsed) is type(expected)


def test_main_passes_explicit_parameter_values_to_report(tmp_path, monkeypatch, capsys):
    param = "entry_band_fraction"
    with_dir = write_report(
        backtest_result([Decimal(3), Decimal(4)], [Decimal(0)] * 2, "0"),
        manifest("with-run", {}, resolved_enabled=0.2, param=param),
        tmp_path,
    )
    without_dir = write_report(
        backtest_result([Decimal(1), Decimal(2)], [Decimal(0)] * 2, "0"),
        manifest("without-run", {param: 1.0}, param=param),
        tmp_path,
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "ablation_compare", "--with", str(with_dir), "--without", str(without_dir),
            "--param", param, "--with-value", "0.2", "--without-value", "1.0", "--seed", "43",
        ],
    )

    main()

    rendered = capsys.readouterr().out
    assert rendered.splitlines()[0] == f"比較: {param} with=0.2 → without=1.0"
    assert "seed=43" in rendered
    assert rendered.splitlines()[-1] == f"verdict: {KEEP}"
