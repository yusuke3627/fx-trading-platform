"""Bar generation from the stored tick series: resume point and idempotency."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.support import T0, FixedClock, at, make_tick
from trading.data.market.bar_service import (
    BarService,
    configured_timeframes,
    foldable_timeframes,
)
from trading.domain.market import TIMEFRAME_SECONDS


class FakeTickStore:
    """Stores ticks and answers the visibility window the way Postgres does:
    event_time >= since AND received_at <= t, ordered by (event_time, arrival).
    """

    def __init__(self, ticks=()) -> None:
        self.ticks = list(ticks)

    def insert_many(self, ticks, *, source, ingestion_run) -> int:
        self.ticks.extend(ticks)
        return len(ticks)

    def known_before(self, symbol, t, since):
        visible = [
            tick
            for tick in self.ticks
            if tick.symbol == symbol and tick.time >= since and tick.known_time <= t
        ]
        return sorted(visible, key=lambda tick: tick.time)


class FakeBarStore:
    """Mirrors the unique key: a bar already stored for a bucket is kept."""

    def __init__(self) -> None:
        self.bars = []

    def insert_many(self, bars) -> int:
        stored = 0
        for bar in bars:
            key = (bar.symbol, bar.timeframe, bar.start)
            if any((b.symbol, b.timeframe, b.start) == key for b in self.bars):
                continue
            self.bars.append(bar)
            stored += 1
        return stored

    def known_before(self, symbol, timeframe, t, count):
        visible = [
            b
            for b in self.bars
            if b.symbol == symbol and b.timeframe == timeframe and b.known_at <= t
        ]
        return sorted(visible, key=lambda b: b.start)[-count:]


def minute_ticks(count: int, *, offset=timedelta(0)):
    """A quote every 20 seconds, broker clock offset from ours by `offset`."""
    return [
        make_tick(
            "158.840",
            "158.844",
            time=at(seconds=20 * i) + offset,
            received_at=at(seconds=20 * i),
        )
        for i in range(count)
    ]


def make_service(ticks, clock=None):
    tick_store = FakeTickStore(ticks)
    bar_store = FakeBarStore()
    return BarService(tick_store, bar_store, clock=clock or FixedClock(at(hours=1))), bar_store


def test_bars_are_folded_from_the_stored_ticks():
    service, bars = make_service(minute_ticks(10))

    assert service.build_once("USDJPY", "1m") == 2

    assert [b.start for b in bars.bars] == [at(minutes=1), at(minutes=2)]
    assert all(b.timeframe == "1m" for b in bars.bars)


def test_the_first_candle_of_a_cold_start_is_dropped():
    # The lookback lands mid-bucket, so the first bar the fold closes may have
    # been entered halfway. The write is idempotent and can never be
    # corrected, so a possibly-partial candle is not persisted at all.
    service, bars = make_service(minute_ticks(10))

    service.build_once("USDJPY", "1m")

    assert at(minutes=0) not in [b.start for b in bars.bars]


def test_a_second_pass_resumes_from_the_last_stored_bar_and_writes_nothing_new():
    service, bars = make_service(minute_ticks(10))
    first = service.build_once("USDJPY", "1m")

    assert service.build_once("USDJPY", "1m") == 0
    assert len(bars.bars) == first


def test_a_later_pass_picks_up_the_ticks_that_arrived_since():
    tick_store = FakeTickStore(minute_ticks(10))
    bars = FakeBarStore()
    clock = FixedClock(at(hours=1))
    service = BarService(tick_store, bars, clock=clock)
    service.build_once("USDJPY", "1m")

    tick_store.ticks.extend(
        make_tick("158.900", "158.904", time=at(seconds=20 * i), received_at=at(seconds=20 * i))
        for i in range(10, 16)
    )

    assert service.build_once("USDJPY", "1m") == 2
    assert [b.start for b in bars.bars] == [
        at(minutes=1),
        at(minutes=2),
        at(minutes=3),
        at(minutes=4),
    ]


def test_bars_carry_the_broker_clock_for_the_candle_and_ours_for_visibility():
    # The whole point of ADR-005: a +3h server offset must not move when the
    # bar becomes visible, and must not move the candle off the broker grid.
    offset = timedelta(hours=3)
    service, bars = make_service(
        minute_ticks(10, offset=offset), clock=FixedClock(at(hours=1))
    )

    service.build_once("USDJPY", "1m")

    bar = bars.bars[0]
    assert bar.start == at(minutes=1) + offset
    assert bar.close_time == at(minutes=2) + offset
    assert bar.known_at == at(minutes=2)


def test_session_anchored_timeframes_are_reported_not_silently_skipped():
    foldable, refused = foldable_timeframes(["1m", "1h", "4h", "1d"])

    assert foldable == ["1m", "1h"]
    assert refused == ["4h", "1d"]


def test_configured_timeframes_are_collected_across_strategies():
    config = _config_with(
        {
            "a": (["USDJPY"], {"entry": "5m"}),
            "b": (["USDJPY"], {"regime": "1h", "entry": "5m"}),
            "c": (["EURUSD"], {"entry": "15m"}),
        }
    )

    assert configured_timeframes(config, "USDJPY") == ["5m", "1h"]


def _config_with(strategies):
    from trading.strategy.base import StrategyConfig

    return SimpleNamespace(
        strategies={
            strategy_id: StrategyConfig(
                strategy_id=strategy_id,
                instruments=instruments,
                timeframes=timeframes,
            )
            for strategy_id, (instruments, timeframes) in strategies.items()
        }
    )


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h"])
def test_every_foldable_timeframe_lands_on_its_own_grid(timeframe):
    seconds = TIMEFRAME_SECONDS[timeframe]
    ticks = [
        make_tick(
            "158.840",
            "158.844",
            time=T0 + timedelta(seconds=seconds // 2 * i),
            received_at=T0 + timedelta(seconds=seconds // 2 * i),
        )
        for i in range(8)
    ]
    service, bars = make_service(ticks, clock=FixedClock(T0 + timedelta(days=1)))

    service.build_once("USDJPY", timeframe)

    for bar in bars.bars:
        assert int(bar.start.timestamp()) % seconds == 0
