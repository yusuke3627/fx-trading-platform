from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.support import (
    T0,
    FakeBarRepository,
    FakeTickRepository,
    FixedClock,
    make_bar,
    make_tick,
    usdjpy_spec,
)
from trading.data.market.stored import StoredMarketData
from trading.domain.position import PositionDirection
from trading.indicators import IndicatorService
from trading.strategy.base import StrategyConfig
from trading.strategy.scalp.failed_spike_reversal import FailedSpikeReversalStrategy


@pytest.mark.parametrize("previous_spread,committed_spread,permits_signal", [
    ("0.002", "0.200", False),
    ("0.200", "0.002", True),
])
def test_spread_gate_uses_the_quote_in_the_evaluated_window(
    previous_spread, committed_spread, permits_signal,
):
    prices = ["150", "150.03", "150.05", "150.08", "150.10",
              "150.20", "150.60", "150.80", "150.40", "150.10"]
    ticks = [
        make_tick(
            price, str(Decimal(price) + Decimal(previous_spread)),
            time=T0 + timedelta(seconds=10 * index),
        )
        for index, price in enumerate(prices)
    ]
    committed = make_tick(
        "150.10", str(Decimal("150.10") + Decimal(committed_spread)),
        time=T0 + timedelta(seconds=100), received_at=T0 + timedelta(seconds=100),
    )
    repository = FakeTickRepository(ticks)
    known_before = repository.known_before

    def commit_then_read(symbol, now, since):
        # CycleClock が固定されていても、受信済み tick の commit は読取間に起こる。
        repository.ticks.append(committed)
        repository.known_before = known_before
        return known_before(symbol, now, since)

    repository.known_before = commit_then_read
    clock = FixedClock(T0 + timedelta(seconds=101))
    bars = [
        make_bar("150", "150.125", "150", "150", start=T0 - timedelta(minutes=i))
        for i in (2, 1)
    ]
    market = StoredMarketData(
        repository, FakeBarRepository(bars), clock, {"USDJPY": usdjpy_spec()},
    )
    context = SimpleNamespace(
        clock=clock, market=market, indicators=IndicatorService(market),
        config=StrategyConfig(
            strategy_id=FailedSpikeReversalStrategy.strategy_id, instruments=["USDJPY"],
            parameters={"atr_period": 1},
        ),
    )

    signal = FailedSpikeReversalStrategy()._evaluate("USDJPY", context)

    assert (signal is not None) is permits_signal
    if signal is not None:
        assert signal.desired_direction is PositionDirection.SHORT
        assert "TICK_MOMENTUM_DOWN" in signal.reason_codes
