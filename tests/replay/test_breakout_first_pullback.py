"""本物のバー構築・Strategy・Risk・OMS・保護/期限決済をつなぐ合成リプレイ。"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.support import make_tick, usdjpy_spec
from trading.backtest.costs import CostModel
from trading.backtest.engine import BacktestEngine
from trading.config import load_config
from trading.domain.risk import EventRiskMode
from trading.strategy.intraday.breakout_first_pullback import BreakoutFirstPullbackStrategy

BREAKOUT = datetime(2026, 1, 14, 10, tzinfo=UTC)


def synthetic_pullback_ticks(exit_kind="profit"):
    ticks = []

    def quote(at, mid):
        price = Decimal(mid)
        ticks.append(make_tick(str(price - Decimal("0.005")), str(price + Decimal("0.005")), time=at))

    for i in range(21 * 12):
        start = BREAKOUT - timedelta(minutes=5 * (21 * 12 - i))
        points = [(0, "149.9"), (60, "150"), (120, "149.8"), (240, "149.9")]
        if i == 21 * 12 - 1:
            points = [(0, "149.9"), (60, "150.25"), (120, "149.8"), (240, "150.2")]
        for seconds, mid in points:
            quote(start + timedelta(seconds=seconds), mid)
    for seconds, mid in [(0, "150.2"), (60, "150.21"), (120, "149.98"),
                         (240, "150.02"), (300, "150.02"), (301, "150.02"), (302, "150.02")]:
        quote(BREAKOUT + timedelta(seconds=seconds), mid)
    if exit_kind == "profit":
        quote(BREAKOUT + timedelta(seconds=303), "150.4")
    elif exit_kind == "stop":
        quote(BREAKOUT + timedelta(seconds=303), "149.5")
    else:
        quote(BREAKOUT + timedelta(seconds=14400 + 302), "150.02")
        quote(BREAKOUT + timedelta(seconds=14400 + 303), "150.02")
        quote(BREAKOUT + timedelta(seconds=14400 + 304), "150.02")
    return ticks


def run_synthetic(exit_kind="profit", *, risk_enabled=True):
    app = load_config("backtest")
    config = app.strategies["breakout_first_pullback"]
    return BacktestEngine(
        risk_config=app.risk.model_copy(update={
            "trading_enabled": risk_enabled, "event_mode_default": EventRiskMode.NORMAL,
        }),
        spec=usdjpy_spec(), costs=CostModel(latency_ms=0, slippage_sigma_pips=0), seed=7,
        strategy_factory=BreakoutFirstPullbackStrategy,
        strategy_config=config.model_copy(update={"enabled": True}), evaluate_from=BREAKOUT,
    ).run(synthetic_pullback_ticks(exit_kind))


@pytest.mark.parametrize("exit_kind,reason", [("profit", "PROTECTION_CLOSE:TAKE_PROFIT"),
                                             ("stop", "PROTECTION_CLOSE:STOP_LOSS"),
                                             ("horizon", "CLOSE")])
def test_real_engine_entry_and_exit(exit_kind, reason):
    result = run_synthetic(exit_kind)
    assert len(result.trades) == 1
    assert result.trades[0].reason == reason
    assert result.trades[0].entry_at >= BREAKOUT + timedelta(minutes=5)
    assert result.trades[0].exit_at > result.trades[0].entry_at
    if exit_kind == "horizon":
        assert result.trades[0].exit_at - result.trades[0].entry_at >= timedelta(hours=4)


def test_real_risk_rejection_does_not_retry_same_breakout():
    result = run_synthetic(risk_enabled=False)
    assert result.trades == []
    assert len(result.risk_rejections) == 1
    assert "TRADING_ENABLED" in result.risk_rejections[0][1]
