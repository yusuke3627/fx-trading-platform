"""Backtest の carry 計上: rollover boundary で PIT swap snapshot を使う。

boundary は server midnight（= NY 17:00、夏は 21:00Z）。snapshot は
`known_at <= boundary` のものだけが使える — 後から集めた snapshot で過去の
rollover に値付けするのは look-ahead（ADR-016）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from tests.support import make_tick, usdjpy_spec
from trading.backtest.costs import CostModel
from trading.backtest.engine import BacktestEngine, ScriptedStrategy
from trading.domain.position import PositionDirection
from trading.domain.risk import EventRiskMode
from trading.domain.swap import SWAP_MODE_POINTS, SwapSnapshot
from trading.risk.engine import RiskConfig
from trading.strategy.base import StrategyConfig

# 2026-08-10 は月曜（EDT）。この日の boundary は 21:00Z。
MONDAY_BOUNDARY = datetime(2026, 8, 10, 21, 0, tzinfo=UTC)
TUESDAY_BOUNDARY = datetime(2026, 8, 11, 21, 0, tzinfo=UTC)


def _risk_config() -> RiskConfig:
    # engine の仕組みを見るテストなので halt 系は開放する（vertical slice と
    # 同じ扱い）。
    return RiskConfig(
        trading_enabled=True,
        max_units_per_symbol={"USDJPY": 10000},
        max_risk_per_trade_pct=Decimal("1.0"),
        portfolio_stop_risk_budget_pct=Decimal(10),
        max_currency_net_exposure_pct=Decimal(1000),
        daily_loss_halt_pct=Decimal(50),
        rolling_24h_loss_halt_pct=Decimal(50),
        high_water_mark_drawdown_halt_pct=Decimal(50),
        event_mode_default=EventRiskMode.NORMAL,
    )


def _snapshot(known_at: datetime, swap_long: str) -> SwapSnapshot:
    return SwapSnapshot(
        snapshot_id=uuid4(),
        symbol="USDJPY",
        swap_mode=SWAP_MODE_POINTS,
        swap_long=Decimal(swap_long),
        swap_short=Decimal("0.4"),
        # MQL5: 3 = Wednesday。テストが跨ぐのは月・火の boundary なので
        # 倍率は常に 1。
        swap_rollover3days=3,
        retrieved_at=known_at,
        known_at=known_at,
    )


def _run(swap_snapshots: list[SwapSnapshot], tick_times: list[datetime]):
    engine = BacktestEngine(
        risk_config=_risk_config(),
        spec=usdjpy_spec(),
        costs=CostModel(),
        seed=7,
        # 2 tick 目で LONG を建て、以後 flip しない（決済は範囲外）。
        strategy_factory=lambda: ScriptedStrategy({1: PositionDirection.LONG}),
        strategy_config=StrategyConfig(
            strategy_id=ScriptedStrategy.strategy_id,
            enabled=True,
            instruments=["USDJPY"],
        ),
        swap_snapshots=swap_snapshots,
        broker_server_ahead_of_ny_hours=7.0,
    )
    ticks = [make_tick("147.000", "147.004", time=t) for t in tick_times]
    return engine.run(ticks)


def _times_across(*boundaries: datetime) -> list[datetime]:
    first = boundaries[0]
    times = [
        first.replace(hour=20, minute=0, second=0),
        first.replace(hour=20, minute=0, second=1),
        first.replace(hour=20, minute=0, second=2),
        first.replace(hour=20, minute=59, second=0),
    ]
    for boundary in boundaries:
        times.append(boundary.replace(minute=5))
        times.append(boundary.replace(minute=6))
    return times


def test_carry_accrues_at_boundary_from_pit_snapshot():
    result = _run(
        [_snapshot(datetime(2026, 8, 10, 6, 0, tzinfo=UTC), "-2.2")],
        _times_across(MONDAY_BOUNDARY),
    )

    (fill,) = result.fills
    # -2.2 points × 0.001 × quantity × 1泊
    expected = Decimal("-2.2") * Decimal("0.001") * fill.quantity
    assert Decimal(result.metrics["carry_total"]) == expected
    assert result.metrics["unpriced_rollovers"] == "0"
    # carry は net に入り、execution_cost（spread/slippage 起因）には
    # 混ざらない。
    net = Decimal(result.metrics["net_pnl"])
    gross = Decimal(result.metrics["gross_mid_pnl"])
    assert Decimal(result.metrics["execution_cost"]) == gross - net + expected


def test_snapshot_known_after_boundary_is_not_used():
    result = _run(
        [_snapshot(MONDAY_BOUNDARY.replace(hour=22), "-2.2")],
        _times_across(MONDAY_BOUNDARY),
    )

    assert result.metrics["carry_total"] == "0"
    assert result.metrics["unpriced_rollovers"] == "1"


def test_each_boundary_uses_its_latest_known_snapshot():
    early = _snapshot(datetime(2026, 8, 10, 6, 0, tzinfo=UTC), "-2.2")
    # 月曜 boundary の後・火曜 boundary の前に改定が届く。
    revised = _snapshot(datetime(2026, 8, 10, 22, 0, tzinfo=UTC), "-9.9")
    result = _run([early, revised], _times_across(MONDAY_BOUNDARY, TUESDAY_BOUNDARY))

    (fill,) = result.fills
    point = Decimal("0.001")
    expected = (Decimal("-2.2") + Decimal("-9.9")) * point * fill.quantity
    assert Decimal(result.metrics["carry_total"]) == expected
    assert result.metrics["unpriced_rollovers"] == "0"


def test_no_snapshots_keeps_carry_zero_and_counts_boundaries():
    result = _run([], _times_across(MONDAY_BOUNDARY))

    # snapshot が 1 つも無い run は carry 0 のまま（従来挙動）だが、値付け
    # できなかった boundary 越えは telemetry に残る — 「carry はゼロだった」
    # と「carry を知らない」を区別する。
    assert result.metrics["carry_total"] == "0"
    assert result.metrics["unpriced_rollovers"] == "1"
    assert len(result.fills) == 1
