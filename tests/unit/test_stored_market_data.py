"""MarketDataService over persisted rows: same visibility rules as the
in-memory store, and a tick window that stays on the broker's clock."""
from datetime import timedelta
from decimal import Decimal

from tests.support import (
    T0,
    FakeBarRepository,
    FakeTickRepository,
    FixedClock,
    at,
    make_bar,
    make_tick,
    usdjpy_spec,
)
from trading.data.market.stored import StoredMarketData

BROKER_OFFSET = timedelta(hours=3)


def make_market(clock, ticks=(), bars=(), instruments=None):
    return StoredMarketData(
        FakeTickRepository(ticks),
        FakeBarRepository(bars),
        clock,
        {"USDJPY": usdjpy_spec()} if instruments is None else instruments,
    )


def hourly_bars(count: int):
    return [
        make_bar(
            "158.80", "158.90", "158.70", "158.85", start=at(hours=hour), timeframe="1h"
        )
        for hour in range(count)
    ]


def test_bars_are_hidden_until_they_are_known():
    clock = FixedClock(T0)
    market = make_market(clock, bars=hourly_bars(3))

    assert market.bars("USDJPY", "1h", 10) == []

    clock.advance(hours=1)
    assert len(market.bars("USDJPY", "1h", 10)) == 1

    clock.advance(hours=2)
    assert len(market.bars("USDJPY", "1h", 10)) == 3


def test_bars_returns_the_most_recent_count_oldest_first():
    market = make_market(FixedClock(at(hours=5)), bars=hourly_bars(3))

    assert [b.start for b in market.bars("USDJPY", "1h", 2)] == [
        at(hours=1),
        at(hours=2),
    ]


def test_latest_tick_is_hidden_until_it_is_received():
    clock = FixedClock(at(minutes=2))
    market = make_market(
        clock,
        ticks=[make_tick("158.840", "158.844", time=at(minutes=1), received_at=at(minutes=5))],
    )

    assert market.latest_tick("USDJPY") is None

    clock.advance(minutes=3)
    assert market.latest_tick("USDJPY") is not None


def test_a_late_old_tick_does_not_rewind_the_latest_price():
    # Reception order is not price order: a quote with an older broker time can
    # be stored after newer ones, and latest must stay on the broker timeline.
    market = make_market(
        FixedClock(at(minutes=6)),
        ticks=[
            make_tick("158.850", "158.854", time=at(minutes=4)),
            make_tick("158.800", "158.804", time=at(minutes=0), received_at=at(minutes=5)),
        ],
    )

    latest = market.latest_tick("USDJPY")
    assert latest is not None and latest.time == at(minutes=4)
    assert [t.time for t in market.ticks("USDJPY", 86400)] == [at(minutes=0), at(minutes=4)]


def test_the_tick_window_is_anchored_on_the_broker_clock():
    # ADR-005: event_time is the broker's clock, ours is offset from it. A
    # window measured from now() would cover a completely different span —
    # here it would let the whole series through.
    ticks = [
        make_tick(
            "158.840",
            "158.844",
            time=at(minutes=minute) + BROKER_OFFSET,
            received_at=at(minutes=minute),
        )
        for minute in (0, 5, 9)
    ]
    market = make_market(FixedClock(at(minutes=10)), ticks=ticks)

    window = market.ticks("USDJPY", 300)

    assert [t.time - BROKER_OFFSET for t in window] == [at(minutes=5), at(minutes=9)]


def test_the_tick_window_holds_while_the_market_is_quiet():
    # A weekend or a collector outage leaves the newest quote hours behind
    # now(). The last known price is still the price.
    market = make_market(
        FixedClock(at(days=2)),
        ticks=[make_tick("158.840", "158.844", time=at(minutes=1))],
    )

    assert len(market.ticks("USDJPY", 60)) == 1
    assert market.latest_tick("USDJPY") is not None


def test_no_ticks_at_all_is_an_empty_window_not_an_error():
    market = make_market(FixedClock(at(minutes=10)))

    assert market.ticks("USDJPY", 60) == []
    assert market.latest_tick("USDJPY") is None


def test_instruments_come_from_the_startup_snapshot():
    market = make_market(FixedClock(T0))

    spec = market.instrument("USDJPY")
    assert spec is not None and spec.pip_size == Decimal("0.01")
    assert market.instrument("EURUSD") is None
