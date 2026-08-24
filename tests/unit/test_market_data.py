"""Replay visibility of the in-memory market data store: with a clock
attached, pre-loaded history must stay invisible until it is known."""
from decimal import Decimal

from tests.support import T0, FixedClock, at, make_bar, make_tick, usdjpy_spec
from trading.data.market import InMemoryMarketData


def loaded_store(clock) -> InMemoryMarketData:
    store = InMemoryMarketData(clock)
    store.set_instrument(usdjpy_spec())
    for hour in range(3):
        store.add_bar(
            make_bar("158.80", "158.90", "158.70", "158.85", start=at(hours=hour), timeframe="1h")
        )
    for minute in (0, 90, 170):
        store.add_tick(make_tick("158.840", "158.844", time=at(minutes=minute)))
    return store


def test_bars_hidden_until_they_are_known():
    clock = FixedClock(T0)
    store = loaded_store(clock)
    # At T0 no bar is known yet.
    assert store.bars("USDJPY", "1h", 10) == []

    clock.advance(hours=1)
    assert len(store.bars("USDJPY", "1h", 10)) == 1

    clock.advance(hours=2)
    assert len(store.bars("USDJPY", "1h", 10)) == 3


def test_ticks_and_latest_tick_respect_clock():
    clock = FixedClock(at(minutes=95))
    store = loaded_store(clock)
    latest = store.latest_tick("USDJPY")
    assert latest is not None and latest.time == at(minutes=90)
    # The future tick at minute 170 is invisible to the window query too.
    assert all(t.time <= at(minutes=95) for t in store.ticks("USDJPY", 86400))


def test_late_received_tick_hidden_until_reception():
    clock = FixedClock(at(minutes=2))
    store = InMemoryMarketData(clock)
    store.add_tick(
        make_tick("158.840", "158.844", time=at(minutes=1), received_at=at(minutes=5))
    )
    assert store.latest_tick("USDJPY") is None

    clock.advance(minutes=3)
    assert store.latest_tick("USDJPY") is not None


def test_late_old_tick_does_not_rewind_latest_price():
    # ReplayEngine delivers in reception order, so a tick with an older
    # broker time can be appended AFTER newer ones; latest must stay on the
    # broker timeline instead of returning the last arrival.
    clock = FixedClock(at(minutes=6))
    store = InMemoryMarketData(clock)
    store.add_tick(make_tick("158.850", "158.854", time=at(minutes=4)))
    store.add_tick(
        make_tick("158.800", "158.804", time=at(minutes=0), received_at=at(minutes=5))
    )
    latest = store.latest_tick("USDJPY")
    assert latest is not None and latest.time == at(minutes=4)
    # The window query is likewise anchored and sorted on broker time.
    window = store.ticks("USDJPY", 86400)
    assert [t.time for t in window] == [at(minutes=0), at(minutes=4)]


def test_without_clock_everything_is_visible():
    store = loaded_store(None)
    assert len(store.bars("USDJPY", "1h", 10)) == 3
    latest = store.latest_tick("USDJPY")
    assert latest is not None and latest.time == at(minutes=170)
    assert store.instrument("USDJPY").pip_size == Decimal("0.01")
