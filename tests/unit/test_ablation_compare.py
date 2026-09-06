import math
from decimal import Decimal

import pytest

from tests.support import at
from trading.backtest.ablation_compare import (
    KEEP,
    REMOVE,
    UNDECIDED_DIFFERENCE,
    UNDECIDED_SAMPLE,
    ArmSummary,
    difference_interval,
    judge,
    load_run,
    report,
)
from trading.backtest.engine import BacktestResult, TradeRecord
from trading.backtest.report import write_report


def arm(count: int, mean: str) -> ArmSummary:
    value = Decimal(mean)
    return ArmSummary(
        count=count,
        total=value * count,
        mean=value,
        hit_rate=0.5,
        max_drawdown=Decimal(10),
        low=float("nan"),
        high=float("nan"),
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


def backtest_result(pnls: list[Decimal], max_drawdown: str) -> BacktestResult:
    trades = [
        TradeRecord(
            strategy_id="post_event_failed_breakout",
            symbol="USDJPY",
            entry_at=at(hours=index),
            exit_at=at(hours=index + 1),
            direction="LONG",
            quantity=Decimal(1000),
            entry_price=Decimal(150),
            exit_price=Decimal(150) + pnl / Decimal(1000),
            net_pnl=pnl,
            reason="CLOSE",
        )
        for index, pnl in enumerate(pnls)
    ]
    return BacktestResult(
        symbol="USDJPY",
        fills=[],
        trades=trades,
        equity_curve=[],
        snapshots=[],
        risk_rejections=[],
        rejected_commands=0,
        metrics={"max_drawdown": max_drawdown},
    )


def manifest(run_id: str, param_overrides: dict) -> dict:
    return {
        "run_id": run_id,
        "git_commit": "0123456789abcdef",
        "git_dirty": False,
        "dataset_hash": "dataset-test-hash",
        "feature_dataset_hash": "feature-test-hash",
        "period_from": "2026-01-01T00:00:00+00:00",
        "period_to": "2026-02-01T00:00:00+00:00",
        "param_overrides": param_overrides,
    }


def test_load_run_and_report_round_trip_trade_pnls_and_provenance(tmp_path):
    with_pnls = [Decimal("12.5"), Decimal("-2.5")]
    without_pnls = [Decimal("1.0"), Decimal("2.0"), Decimal("3.0")]
    with_dir = write_report(
        backtest_result(with_pnls, "4.5"),
        manifest("with-run", {}),
        tmp_path,
    )
    without_dir = write_report(
        backtest_result(without_pnls, "6.5"),
        manifest("without-run", {"macro_confirmation_enabled": False}),
        tmp_path,
    )

    with_run = load_run(with_dir)
    without_run = load_run(without_dir)
    rendered = report(with_run, without_run, seed=42)

    assert with_run.pnls == with_pnls
    assert without_run.pnls == without_pnls
    assert 'param_overrides        {"macro_confirmation_enabled": false}' in rendered
    assert "with-run" in rendered
    assert "without-run" in rendered
    assert "verdict:" in rendered
    assert b"\r\n" not in (with_dir / "trades.csv").read_bytes()


def test_load_run_reports_a_missing_trades_file(tmp_path):
    run_dir = write_report(
        backtest_result([], "0"), manifest("missing-trades", {}), tmp_path
    )
    (run_dir / "trades.csv").unlink()

    with pytest.raises(SystemExit, match="trades.csv"):
        load_run(run_dir)
