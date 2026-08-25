"""ReplayMarketData: O(window) reads under the engine's ordered-feed contract."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading.backtest.market import ReplayMarketData
from trading.domain.market import Tick

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def tick(seconds: int, bid: str = "157.000") -> Tick:
    at = T0 + timedelta(seconds=seconds)
    return Tick(
        symbol="USDJPY", bid=Decimal(bid), ask=Decimal(bid) + Decimal("0.005"),
        time=at, received_at=at,
    )


def test_ticks_answers_the_trailing_window_in_time_order():
    market = ReplayMarketData()
    for s in range(0, 300, 10):
        market.add_tick(tick(s))

    window = market.ticks("USDJPY", 60)

    assert [t.time for t in window] == [
        T0 + timedelta(seconds=s) for s in range(230, 300, 10)
    ]
    assert market.latest_tick("USDJPY").time == T0 + timedelta(seconds=290)


def test_ticks_older_than_the_horizon_are_dropped():
    market = ReplayMarketData(tick_horizon_seconds=100)
    market.add_tick(tick(0))
    market.add_tick(tick(200))

    # The old tick left the store entirely, so even a horizon-wide window
    # cannot see it.
    assert [t.time for t in market.ticks("USDJPY", 100)] == [
        T0 + timedelta(seconds=200)
    ]


def test_a_window_wider_than_the_horizon_is_refused():
    market = ReplayMarketData(tick_horizon_seconds=100)
    market.add_tick(tick(0))

    with pytest.raises(ValueError):
        market.ticks("USDJPY", 101)
