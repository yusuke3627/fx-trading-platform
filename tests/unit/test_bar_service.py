"""Bar generation from the stored tick series: resume point and idempotency."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.support import (
    T0,
    FakeBarRepository,
    FakeTickRepository,
    FixedClock,
    at,
    make_bar,
    make_tick,
)
from trading.data.market.bar_service import BarService, configured_timeframes
from trading.domain.market import TIMEFRAME_SECONDS


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
    tick_store = FakeTickRepository(ticks)
    bar_store = FakeBarRepository()
    return BarService(tick_store, bar_store, clock=clock or FixedClock(at(hours=1))), bar_store


class CountingTicks(FakeTickRepository):
    """Counts window reads. What a pass costs when it cannot publish anything
    is the point of the check under test, and the count is the only way to see
    it from outside.
    """

    def __init__(self, ticks):
        super().__init__(ticks)
        self.window_reads = 0

    def known_before(self, symbol, t, since):
        self.window_reads += 1
        return super().known_before(symbol, t, since)


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
    tick_store = FakeTickRepository(minute_ticks(10))
    bars = FakeBarRepository()
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


def test_a_pass_whose_bucket_is_still_open_does_not_read_the_series():
    # Half a day of quotes into a 1d bucket: folding them could only rebuild
    # what is already there. Reading them to find that out — every interval,
    # growing as the day goes on — is the cost this avoids.
    ticks = CountingTicks(
        [
            make_tick("158.840", "158.844", time=at(hours=h), received_at=at(hours=h))
            for h in range(12)
        ]
    )
    service = BarService(ticks, FakeBarRepository(), clock=FixedClock(at(hours=12)))

    assert service.build_once("USDJPY", "1d") == 0
    assert ticks.window_reads == 0


def test_an_empty_stretch_after_the_last_bar_does_not_force_a_read():
    # The stored bar ends where a market closure begins, so `since` sits on a
    # stretch the series skipped entirely. The bucket actually waiting is the
    # one quoting reopened into, and it is still open — deciding from `since`
    # instead would re-read everything since the reopen, every interval.
    ticks = CountingTicks(
        [
            make_tick(
                "158.840",
                "158.844",
                time=at(days=3, hours=h),
                received_at=at(days=3, hours=h),
            )
            for h in range(12)
        ]
    )
    stored = FakeBarRepository([make_bar("1", "1", "1", "1", timeframe="1d")])
    service = BarService(ticks, stored, clock=FixedClock(at(days=3, hours=12)))

    assert service.build_once("USDJPY", "1d") == 0
    assert ticks.window_reads == 0


def test_the_series_is_read_once_the_pending_bucket_has_ended():
    # The counterpart: a quote past the bucket's end is the proof BarBuilder
    # needs, so the pass goes ahead and publishes.
    ticks = CountingTicks(
        [
            make_tick("158.840", "158.844", time=at(hours=h), received_at=at(hours=h))
            for h in (0, 12, 24)
        ]
    )
    stored = FakeBarRepository(
        [make_bar("1", "1", "1", "1", start=at(days=-1), timeframe="1d")]
    )
    service = BarService(ticks, stored, clock=FixedClock(at(hours=24)))

    assert service.build_once("USDJPY", "1d") == 1
    assert ticks.window_reads == 1
    assert stored.bars[-1].start == T0


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


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"])
def test_every_timeframe_lands_on_its_own_grid(timeframe):
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
    # Long enough for every quote to be visible whatever the timeframe: a
    # fixed horizon would hide the later ones for 4h and 1d and leave nothing
    # to assert on.
    service, bars = make_service(
        ticks, clock=FixedClock(T0 + timedelta(seconds=seconds * 4))
    )

    service.build_once("USDJPY", timeframe)

    assert bars.bars
    for bar in bars.bars:
        assert int(bar.start.timestamp()) % seconds == 0
