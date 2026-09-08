"""Tick -> Bar folding: only closed bars exist, and they close on the grid."""
from datetime import timedelta
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
        known_at=at(seconds=60),
    )


def test_bar_closes_on_the_grid():
    # end_at is stored from Bar.close_time, so it has to land exactly on the
    # bucket end rather than on the last quote inside it.
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


def test_a_late_quote_joins_its_own_bucket_and_delays_the_bars_known_at():
    # The broker time falls inside the first minute, but the quote only
    # reached us during the third. Bucketing is on the broker clock, so it
    # belongs to that bar; what moves is known_at, and that is what stops a
    # replay from reading the candle before its contents had arrived.
    builder = BarBuilder("USDJPY", "1m")
    builder.on_tick(make_tick("158.840", "158.844", time=T0))
    builder.on_tick(
        make_tick("159.500", "159.504", time=at(seconds=30), received_at=at(minutes=2))
    )

    bar = builder.on_tick(make_tick("158.850", "158.854", time=at(minutes=1)))
    assert bar is not None
    assert bar.start == T0
    assert bar.high == Decimal("159.500")
    assert bar.tick_volume == 2
    assert bar.known_at == at(minutes=2)


def test_a_straggler_from_an_older_bucket_neither_joins_nor_closes():
    # The quote belongs to a bar published long ago. On the broker's clock it
    # says nothing about the bar currently open, so it releases nothing; it
    # stays in the tick series.
    builder = BarBuilder("USDJPY", "1m")
    builder.on_tick(make_tick("158.840", "158.844", time=at(minutes=1)))

    assert (
        builder.on_tick(
            make_tick(
                "159.500", "159.504", time=at(seconds=30), received_at=at(minutes=2, seconds=10)
            )
        )
        is None
    )

    bar = builder.on_tick(make_tick("158.850", "158.854", time=at(minutes=2)))
    assert bar is not None
    assert bar.start == at(minutes=1)
    assert bar.high == Decimal("158.840")
    assert bar.tick_volume == 1


def test_a_quote_stamped_past_the_end_closes_the_bar_whenever_it_arrives():
    # Only the broker's clock decides that a minute is over. A quote stamped
    # in the next minute closes this one even though it reached us before the
    # minute had elapsed on our own clock — under a server offset that is the
    # normal case, not an anomaly.
    builder = BarBuilder("USDJPY", "1m")
    builder.on_tick(make_tick("158.840", "158.844", time=at(seconds=10)))

    bar = builder.on_tick(
        make_tick(
            "159.900", "159.904", time=at(minutes=1, seconds=5), received_at=at(seconds=40)
        )
    )
    assert bar is not None
    assert bar.start == T0
    assert bar.high == Decimal("158.840")  # the next minute's quote stayed out
    assert bar.tick_volume == 1
    # known_at is a reception, never the broker's stamp.
    assert bar.known_at == at(seconds=40)


def test_a_constant_broker_offset_still_produces_bars():
    # OANDA's server runs UTC+3 and labels its timestamps UTC, so every
    # quote's broker stamp is hours ahead of its reception. Deciding the
    # bucket's end on the reception clock made that condition unreachable and
    # the builder emitted nothing at all.
    builder = BarBuilder("USDJPY", "1m")
    offset = timedelta(hours=3)
    published = [
        bar
        for bar in (
            builder.on_tick(
                make_tick(
                    "158.840",
                    "158.844",
                    time=at(seconds=30 * i) + offset,
                    received_at=at(seconds=30 * i),
                )
            )
            for i in range(6)
        )
        if bar is not None
    ]

    assert [b.start for b in published] == [T0 + offset, at(minutes=1) + offset]
    # The candle sits on their clock; its visibility sits on ours.
    assert published[0].close_time == at(minutes=1) + offset
    assert published[0].known_at == at(minutes=1)


def test_a_broker_clock_that_steps_back_keeps_publishing_bars():
    # The server leaves summer time and its wall clock drops an hour. Every
    # quote after that is stamped before the open bucket, so it neither folds
    # nor closes it: without a rule for the jump the builder emits nothing
    # until the clock has climbed back, an hour of candles gone. Our own
    # clock does not move, so known_at keeps rising throughout.
    builder = BarBuilder("USDJPY", "1m")
    ticks = [
        make_tick("158.840", "158.844", time=T0, received_at=T0),
        make_tick(
            "158.850",
            "158.854",
            time=at(seconds=30),
            received_at=at(seconds=30),
        ),
        make_tick(
            "158.860",
            "158.864",
            time=at(minutes=1),
            received_at=at(minutes=1),
        ),
        make_tick(
            "158.870",
            "158.874",
            time=at(minutes=1, seconds=30),
            received_at=at(minutes=1, seconds=30),
        ),
    ]
    stepped_back = at(minutes=2) - timedelta(hours=1)
    ticks.extend(
        make_tick(
            f"157.{index:03}",
            f"158.{index:03}",
            time=stepped_back + timedelta(seconds=30 * index),
            received_at=at(minutes=2) + timedelta(seconds=30 * index),
        )
        for index in range(10)
    )

    published = [bar for tick in ticks if (bar := builder.on_tick(tick)) is not None]

    assert [bar.start for bar in published] == [
        T0,
        at(minutes=1),
        at(minutes=2) - timedelta(hours=1),
        at(minutes=3) - timedelta(hours=1),
        at(minutes=4) - timedelta(hours=1),
        at(minutes=5) - timedelta(hours=1),
    ]
    assert published[0].tick_volume == 2
    assert published[1].tick_volume == 2
    assert published[2].open == Decimal("157.000")
    known_times = [bar.known_at for bar in published]
    assert known_times == sorted(known_times)


def test_a_quote_less_than_the_step_threshold_behind_is_still_a_straggler():
    # The width of the jump is the only thing that separates a clock that
    # moved from a quote that arrived late, so the threshold has to hold: a
    # single stale quote must not close the open bucket and restart the
    # builder twenty minutes in the past.
    builder = BarBuilder("USDJPY", "1m")
    builder.on_tick(
        make_tick(
            "158.840",
            "158.844",
            time=at(minutes=40),
            received_at=at(minutes=40),
        )
    )

    late = make_tick(
        "159.500",
        "159.504",
        time=at(minutes=20),
        received_at=at(minutes=40, seconds=30),
    )
    assert builder.on_tick(late) is None

    bar = builder.on_tick(
        make_tick(
            "158.850",
            "158.854",
            time=at(minutes=41),
            received_at=at(minutes=41),
        )
    )
    assert bar is not None
    assert bar.high == Decimal("158.840")
    assert bar.tick_volume == 1


def test_a_step_back_inside_the_open_bucket_only_folds():
    # An hour is narrower than a 4h candle, so the same jump can land inside
    # the bucket that is already open. Nothing is over, and the quote is
    # simply folded — on broker time, so the earlier stamp takes the open
    # rather than the close.
    builder = BarBuilder("USDJPY", "4h")
    builder.on_tick(
        make_tick(
            "158.840",
            "158.844",
            time=at(hours=6),
            received_at=at(hours=6),
        )
    )

    assert (
        builder.on_tick(
            make_tick(
                "159.500",
                "159.504",
                time=at(hours=5),
                received_at=at(hours=7),
            )
        )
        is None
    )

    bar = builder.on_tick(
        make_tick(
            "158.900",
            "158.904",
            time=at(hours=8),
            received_at=at(hours=8),
        )
    )
    assert bar is not None
    assert bar.open == Decimal("159.500")
    assert bar.high == Decimal("159.500")
    assert bar.close == Decimal("158.840")
    assert bar.tick_volume == 2


def test_a_leading_quote_under_clock_skew_seeds_the_next_bar():
    # Under skew a quote stamped in the next minute can reach us before that
    # minute has elapsed on our clock. It closes the open bar without joining
    # it, and it is not dropped either: it opens the bucket it belongs to and
    # sets that candle's first price.
    builder = BarBuilder("USDJPY", "1m")
    offset = timedelta(hours=3)
    builder.on_tick(
        make_tick(
            "158.800",
            "158.804",
            time=at(seconds=10) + offset,
            received_at=at(seconds=10),
        )
    )

    first = builder.on_tick(
        make_tick(
            "159.500",
            "159.504",
            time=at(minutes=1, seconds=5) + offset,
            received_at=at(seconds=30),
        )
    )
    assert first is not None
    assert first.high == Decimal("158.800")

    builder.on_tick(
        make_tick(
            "158.100",
            "158.104",
            time=at(minutes=1, seconds=40) + offset,
            received_at=at(minutes=1, seconds=40),
        )
    )
    second = builder.on_tick(
        make_tick(
            "158.900",
            "158.904",
            time=at(minutes=2) + offset,
            received_at=at(minutes=2),
        )
    )
    assert second is not None
    assert second.open == Decimal("159.500")
    assert second.high == Decimal("159.500")
    assert second.low == Decimal("158.100")


def test_a_skewed_series_loses_no_bar_and_no_extreme():
    # A whole series under a three-hour offset: every minute that held quotes
    # prints its candle, every quote lands in one of them, and each high and
    # low is the extreme of its own minute. Backtests are folded from this,
    # so a bar quietly missing or clipped changes what research measures.
    builder = BarBuilder("USDJPY", "1m")
    offset = timedelta(hours=3)
    quote_groups = [
        ("158.800", "158.950", "158.700"),
        ("158.810", "159.100", "158.650"),
        ("158.820", "159.250", "158.600"),
        ("158.830", "159.400", "158.550"),
        ("158.840", "159.550", "158.500"),
    ]
    published = []
    for minute, bids in enumerate(quote_groups):
        for second, bid in zip((0, 20, 40), bids, strict=True):
            tick_time = at(minutes=minute, seconds=second)
            bar = builder.on_tick(
                make_tick(
                    bid,
                    "160.000",
                    time=tick_time + offset,
                    received_at=tick_time,
                )
            )
            if bar is not None:
                published.append(bar)

    closing = builder.on_tick(
        make_tick(
            "158.900",
            "160.000",
            time=at(minutes=5) + offset,
            received_at=at(minutes=5),
        )
    )
    assert closing is not None
    published.append(closing)

    assert [bar.start for bar in published] == [
        at(minutes=minute) + offset for minute in range(5)
    ]
    assert [bar.high for bar in published] == [
        max(Decimal(bid) for bid in bids) for bids in quote_groups
    ]
    assert [bar.low for bar in published] == [
        min(Decimal(bid) for bid in bids) for bids in quote_groups
    ]
    assert sum(bar.tick_volume for bar in published) == 15


def test_out_of_order_quotes_in_a_bucket_take_open_and_close_from_broker_time():
    # A reconnect flushes an older quote after a newer one, both still inside
    # the minute and both known before it closes. High and low do not care
    # about order, but open and close do: taking them from arrival order
    # would hand indicators a close that the market printed 30s earlier.
    builder = BarBuilder("USDJPY", "1m")
    builder.on_tick(make_tick("158.900", "158.904", time=at(seconds=50)))
    builder.on_tick(
        make_tick("158.700", "158.704", time=at(seconds=20), received_at=at(seconds=55))
    )

    bar = builder.on_tick(make_tick("158.800", "158.804", time=at(minutes=1)))
    assert bar is not None
    assert bar.open == Decimal("158.700")  # 00:00:20, received second
    assert bar.close == Decimal("158.900")  # 00:00:50, received first
    assert bar.high == Decimal("158.900")
    assert bar.low == Decimal("158.700")
    assert bar.tick_volume == 2


def test_a_quote_from_a_later_bucket_closes_the_open_bar_and_seeds_its_own():
    # Broker time 00:02:30 is proof the 00:00 bar is over. The quote does not
    # belong to that bar, so it opens the bucket it does belong to; the
    # minutes in between held no ticks and print no candle.
    builder = BarBuilder("USDJPY", "1m")
    builder.on_tick(make_tick("158.840", "158.844", time=T0))

    bar = builder.on_tick(
        make_tick("159.500", "159.504", time=at(minutes=2, seconds=30), received_at=at(minutes=4))
    )
    assert bar is not None
    assert bar.start == T0
    assert bar.high == Decimal("158.840")
    assert bar.tick_volume == 1

    second = builder.on_tick(make_tick("158.900", "158.904", time=at(minutes=3)))
    assert second is not None
    assert second.start == at(minutes=2)
    assert second.high == Decimal("159.500")
    # Its own reception, not the closing quote's, is the later of the two.
    assert second.known_at == at(minutes=4)


def test_four_hour_bars_fold_on_the_trade_servers_own_grid():
    # tick.time is the server's wall clock labelled UTC (ADR-005), so the
    # bucket a quote joins is the server's candle without an anchor having to
    # be known. 05:30 belongs to the 04:00 candle, not to one opened wherever
    # the first quote happened to land.
    builder = BarBuilder("USDJPY", "4h")
    assert builder.on_tick(make_tick("158.840", "158.844", time=at(hours=5, minutes=30))) is None

    bar = builder.on_tick(make_tick("158.900", "158.904", time=at(hours=8)))
    assert bar is not None
    assert bar.start == at(hours=4)
    assert bar.close_time == at(hours=8)


def test_daily_bars_fold_on_the_trade_servers_midnight():
    builder = BarBuilder("USDJPY", "1d")
    for hour in (0, 13, 23):
        assert builder.on_tick(make_tick("158.840", "158.844", time=at(hours=hour))) is None

    bar = builder.on_tick(make_tick("158.900", "158.904", time=at(days=1)))
    assert bar is not None
    assert bar.start == T0
    assert bar.close_time == at(days=1)
    assert bar.tick_volume == 3


def test_a_quote_stamped_exactly_on_the_boundary_belongs_to_the_next_bar():
    # The grid is half-open: 00:01:00 closes the 00:00 bar rather than joining
    # it, and becomes the first quote of the one that follows.
    builder = BarBuilder("USDJPY", "1m")
    builder.on_tick(make_tick("158.840", "158.844", time=T0))

    bar = builder.on_tick(make_tick("158.900", "158.904", time=at(minutes=1)))
    assert bar is not None
    assert bar.high == Decimal("158.840")
    assert bar.tick_volume == 1

    second = builder.on_tick(make_tick("158.850", "158.854", time=at(minutes=2)))
    assert second is not None
    assert second.start == at(minutes=1)
    assert second.open == Decimal("158.900")


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
