"""Tick -> Bar folding: only closed bars exist, and they close on the grid."""
from decimal import Decimal

from tests.support import T0, at, make_tick
from trading.data.market.bars import BarBuilder
from trading.domain.market import Bar
from trading.strategy.base import TimeframeMap


def test_bar_is_not_published_until_its_bucket_closes():
    builder = BarBuilder("USDJPY", "1m")
    assert builder.on_tick(make_tick("158.840", "158.844", time=T0)) is None
    assert builder.on_tick(make_tick("158.850", "158.854", time=at(seconds=59))) is None
    # The first tick of the next minute is what closes the first bar.
    bar = builder.on_tick(make_tick("158.860", "158.864", time=at(seconds=60)))
    assert bar is not None
    assert bar.start == T0


def test_completed_bar_folds_the_bid_series_of_its_bucket():
    builder = BarBuilder("USDJPY", "1m")
    for bid, second in (("158.840", 0), ("158.870", 10), ("158.820", 20), ("158.850", 30)):
        assert builder.on_tick(make_tick(bid, "159.000", time=at(seconds=second))) is None

    bar = builder.on_tick(make_tick("158.900", "159.000", time=at(seconds=60)))
    assert bar == Bar(
        symbol="USDJPY",
        timeframe="1m",
        start=T0,
        open=Decimal("158.840"),
        high=Decimal("158.870"),
        low=Decimal("158.820"),
        close=Decimal("158.850"),
        tick_volume=4,
    )


def test_bar_is_known_exactly_at_its_close():
    # market_bars stores known_at = end_at, and Bar.close_time is the single
    # source of that value, so it has to land on the bucket end.
    builder = BarBuilder("USDJPY", "5m")
    builder.on_tick(make_tick("158.840", "158.844", time=T0))
    bar = builder.on_tick(make_tick("158.850", "158.854", time=at(minutes=5)))
    assert bar is not None
    assert bar.close_time == at(minutes=5)


def test_bucket_start_is_aligned_to_the_timeframe_grid():
    # A feed that starts mid-candle must not offset the grid, or every bar
    # would disagree with the broker's own.
    builder = BarBuilder("USDJPY", "5m")
    builder.on_tick(make_tick("158.840", "158.844", time=at(minutes=7, seconds=13)))
    bar = builder.on_tick(make_tick("158.850", "158.854", time=at(minutes=10)))
    assert bar is not None
    assert bar.start == at(minutes=5)
    assert bar.close_time == at(minutes=10)


def test_buckets_without_ticks_produce_no_bars():
    # A quiet market prints no candle; inventing empty bars would feed
    # indicators prices that never traded.
    builder = BarBuilder("USDJPY", "1m")
    builder.on_tick(make_tick("158.840", "158.844", time=T0))

    first = builder.on_tick(make_tick("158.900", "158.904", time=at(minutes=5)))
    assert first is not None and first.start == T0

    # Minutes 1 to 4 held no ticks and produced nothing; the next bar is the
    # one that actually had them.
    second = builder.on_tick(make_tick("158.910", "158.914", time=at(minutes=6)))
    assert second is not None and second.start == at(minutes=5)


def test_late_tick_for_a_closed_bucket_is_dropped():
    # Replay delivers in reception order, so a tick with an older broker time
    # can arrive after its bucket closed. Folding it in would rewrite a candle
    # a strategy may already have traded on.
    builder = BarBuilder("USDJPY", "1m")
    builder.on_tick(make_tick("158.840", "158.844", time=T0))
    first = builder.on_tick(make_tick("158.900", "158.904", time=at(minutes=1)))
    assert first is not None and first.high == Decimal("158.840")

    late = make_tick(
        "159.500", "159.504", time=at(seconds=30), received_at=at(minutes=1, seconds=30)
    )
    assert builder.on_tick(late) is None

    # It did not leak into the bucket that is open either.
    second = builder.on_tick(make_tick("158.910", "158.914", time=at(minutes=2)))
    assert second is not None and second.high == Decimal("158.900")


def test_trailing_incomplete_bucket_is_never_published():
    # There is no flush(): the last, still-open bucket has no completed bar,
    # so a run that ends mid-candle simply has one fewer bar.
    builder = BarBuilder("USDJPY", "1m")
    for second in (0, 20, 40):
        assert builder.on_tick(make_tick("158.840", "158.844", time=at(seconds=second))) is None


def test_configured_timeframes_are_ordered_by_duration():
    # Bar builders are wired from this, so the order has to be a property of
    # the configuration's content, not of its key order.
    assert TimeframeMap(entry="5m", regime="1h", setup="15m").all() == ("5m", "15m", "1h")
    assert TimeframeMap(setup="15m", regime="1h", entry="5m").all() == ("5m", "15m", "1h")


def test_timeframes_shared_by_several_roles_build_one_series():
    assert TimeframeMap(setup="1m", entry="1m").all() == ("1m",)


def test_a_strategy_without_timeframes_has_none():
    assert TimeframeMap().all() == ()
