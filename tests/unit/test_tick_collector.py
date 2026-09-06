"""Tick collector: broker fetch failures must never read as an empty feed,
and reception time must be stamped by the injected clock so the stored series
stays point-in-time."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from types import SimpleNamespace

import pytest

from tests.support import FixedClock, make_tick
from trading.data.market import collector as collector_module
from trading.data.market.bars import BarBuilder
from trading.data.market.collector import TickCollector
from trading.execution.mt5.adapter import MT5ConnectionError

SYMBOL = "USDJPY"
T0 = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
T0_MSC = int(T0.timestamp() * 1000)


def info_tick(time_msc: int, bid: str, ask: str):
    """symbol_info_tick returns an object read by attribute."""
    return SimpleNamespace(time_msc=time_msc, bid=float(bid), ask=float(ask))


def range_row(time_msc: int, bid: str, ask: str) -> dict:
    """copy_ticks_range returns a numpy structured array whose records are
    read by key, never by attribute. A SimpleNamespace here would let an
    attribute-based mapping pass the tests and fail on the trading host."""
    return {"time_msc": time_msc, "bid": float(bid), "ask": float(ask)}


class FakeMT5:
    def __init__(
        self,
        *,
        info_ticks=(),
        range_rows=(),
        on_range_call=None,
        initialize_ok: bool = True,
        select_ok: bool = True,
    ) -> None:
        self._info_ticks = list(info_ticks)
        # None is a failed fetch, not an empty one: the terminal reports both
        # and the collector has to tell them apart.
        self._range_rows = range_rows
        self._on_range_call = on_range_call
        self._initialize_ok = initialize_ok
        self._select_ok = select_ok
        self.range_calls: list[tuple] = []
        self.selected: list[tuple] = []
        self.shutdown_calls = 0

    def initialize(self) -> bool:
        return self._initialize_ok

    def symbol_select(self, symbol, enable) -> bool:
        self.selected.append((symbol, enable))
        return self._select_ok

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def symbol_info_tick(self, symbol):
        if not self._info_ticks:
            return None
        # The last quote keeps being returned, as a real terminal does
        # between two updates.
        return self._info_ticks.pop(0) if len(self._info_ticks) > 1 else self._info_ticks[0]

    def copy_ticks_range(self, symbol, date_from, date_to, flags):
        self.range_calls.append((symbol, date_from, date_to, flags))
        if self._on_range_call is not None:
            self._on_range_call()
        if self._range_rows is None:
            return None
        if date_from == date_to:
            return [r for r in self._range_rows if _row_time(r) == date_from]
        return [r for r in self._range_rows if date_from <= _row_time(r) < date_to]

    def last_error(self):
        return (-10004, "no connection")


def _row_time(row: dict) -> datetime:
    return datetime.fromtimestamp(row["time_msc"] / 1000, tz=UTC)


class FakeTickRepository:
    def __init__(self) -> None:
        self.ticks = []
        self.batches: list[list] = []
        self.calls: list[dict] = []

    def insert_many(self, ticks, *, source, ingestion_run) -> int:
        self.ticks.extend(ticks)
        self.batches.append(list(ticks))
        self.calls.append({"source": source, "ingestion_run": ingestion_run})
        return len(ticks)

    def known_before(self, symbol, t, since):
        return []


def make_collector(mt5, clock=None):
    repository = FakeTickRepository()
    collector = TickCollector(
        repository, clock=clock or FixedClock(T0), mt5_module=mt5
    )
    return collector, repository


def test_raw_tick_is_mapped_and_stamped_with_reception_time():
    clock = FixedClock(T0)
    collector, repository = make_collector(
        FakeMT5(info_ticks=[info_tick(T0_MSC - 250, "158.840", "158.844")]), clock
    )

    collector.poll_once(SYMBOL)

    stored = repository.ticks[0]
    assert stored.symbol == SYMBOL
    assert stored.bid == Decimal("158.840")
    assert stored.ask == Decimal("158.844")
    # Broker time comes from the millisecond field, reception from the clock.
    assert stored.time == T0 - timedelta(milliseconds=250)
    assert stored.received_at == clock.now()


def test_poll_once_reports_rows_actually_stored():
    collector, repository = make_collector(
        FakeMT5(info_ticks=[info_tick(T0_MSC, "158.840", "158.844")])
    )

    assert collector.poll_once(SYMBOL) == 1
    assert len(repository.ticks) == 1


def test_unchanged_quote_is_not_written_again():
    collector, repository = make_collector(
        FakeMT5(info_ticks=[info_tick(T0_MSC, "158.840", "158.844")])
    )

    collector.poll_once(SYMBOL)

    assert collector.poll_once(SYMBOL) == 0
    assert len(repository.ticks) == 1


def test_repeat_suppression_is_per_symbol():
    # The fake serves one quote for every symbol, so a shared "last quote"
    # would treat the second symbol's first tick as a repeat and drop it.
    collector, repository = make_collector(
        FakeMT5(info_ticks=[info_tick(T0_MSC, "158.840", "158.844")])
    )

    collector.poll_once("USDJPY")
    collector.poll_once("EURJPY")

    assert [t.symbol for t in repository.ticks] == ["USDJPY", "EURJPY"]


def test_second_quote_within_the_same_second_is_kept():
    # Both quotes fall in the same second: read at second resolution they
    # would carry an identical event_time, losing their order and making a
    # repeat of the same quote indistinguishable from a new one.
    collector, repository = make_collector(
        FakeMT5(
            info_ticks=[
                info_tick(T0_MSC, "158.840", "158.844"),
                info_tick(T0_MSC + 500, "158.845", "158.849"),
            ]
        )
    )

    collector.poll_once(SYMBOL)
    collector.poll_once(SYMBOL)

    assert len(repository.ticks) == 2
    first, second = repository.ticks
    assert second.time - first.time == timedelta(milliseconds=500)


def test_failed_quote_fetch_raises_instead_of_reporting_no_tick():
    collector, repository = make_collector(FakeMT5(info_ticks=[]))

    with pytest.raises(MT5ConnectionError):
        collector.poll_once(SYMBOL)
    assert repository.ticks == []


def test_quotes_missed_between_two_polls_are_filled_from_the_tick_history():
    # symbol_info_tick hands back one quote per call, so a burst arriving
    # faster than the poll rate is only ever sampled. The hole between the
    # previous quote and this one is read from the tick history straight
    # after, which is what makes the stored series complete rather than
    # sampled.
    clock = FixedClock(T0)
    prices = [
        ("158.840", "158.844"),
        ("158.845", "158.849"),
        ("158.850", "158.854"),
        ("158.855", "158.859"),
        ("158.860", "158.864"),
        ("158.865", "158.869"),
        ("158.870", "158.874"),
        ("158.875", "158.879"),
        ("158.880", "158.884"),
    ]
    rows = [
        range_row(T0_MSC + index * 100, bid, ask)
        for index, (bid, ask) in enumerate(prices)
    ]
    # The range overlaps the quote already stored by the first poll, and the
    # terminal is free to repeat a row. Neither may inflate the series: the
    # unique key would absorb them in Postgres, but the count this returns is
    # what the caller reads.
    mt5 = FakeMT5(
        info_ticks=[
            info_tick(T0_MSC, *prices[0]),
            info_tick(T0_MSC + 800, *prices[-1]),
        ],
        range_rows=[rows[0], *rows],
    )
    collector, repository = make_collector(mt5, clock)

    assert collector.poll_once(SYMBOL) == 1
    clock.advance(seconds=1)
    assert collector.poll_once(SYMBOL) == 8

    assert len(repository.ticks) == 9
    assert [tick.time for tick in repository.ticks] == sorted(
        tick.time for tick in repository.ticks
    )
    assert [tick.bid for tick in repository.ticks] == [
        Decimal(bid) for bid, _ask in prices
    ]
    assert {tick.received_at for tick in repository.ticks[1:]} == {clock.now()}
    assert [len(batch) for batch in repository.batches] == [1, 8]


def test_a_burst_that_polling_samples_still_yields_the_full_high_and_low():
    # Why the hole matters: the high and the low of the burst land between
    # two polls, and a bar folded from the sampled series would print neither.
    # A release or an intervention is exactly when those extremes are the
    # point, and ON CONFLICT DO NOTHING means a bar written without them can
    # never be corrected.
    clock = FixedClock(T0)
    prices = [
        ("158.840", "158.844"),
        ("158.900", "158.904"),
        ("159.500", "159.504"),
        ("158.700", "158.704"),
        ("158.100", "158.104"),
        ("158.750", "158.754"),
        ("159.200", "159.204"),
        ("158.600", "158.604"),
        ("158.850", "158.854"),
    ]
    rows = [
        range_row(T0_MSC + index * 100, bid, ask)
        for index, (bid, ask) in enumerate(prices)
    ]
    collector, repository = make_collector(
        FakeMT5(
            info_ticks=[
                info_tick(T0_MSC, *prices[0]),
                info_tick(T0_MSC + 800, *prices[-1]),
            ],
            range_rows=rows,
        ),
        clock,
    )

    collector.poll_once(SYMBOL)
    clock.advance(seconds=1)
    collector.poll_once(SYMBOL)

    builder = BarBuilder(SYMBOL, "1m")
    for tick in repository.ticks:
        assert builder.on_tick(tick) is None
    bar = builder.on_tick(
        make_tick(
            "158.860",
            "158.864",
            time=T0 + timedelta(minutes=1),
            received_at=T0 + timedelta(seconds=2),
        )
    )
    assert bar is not None
    assert bar.high == Decimal("159.500")
    assert bar.low == Decimal("158.100")


def test_an_unchanged_quote_costs_no_tick_history_call():
    # Most polls re-read the quote they already have. Those must stay a
    # single round trip, or repairing the holes would cost a second call
    # five times a second to fetch nothing.
    mt5 = FakeMT5(info_ticks=[info_tick(T0_MSC, "158.840", "158.844")])
    collector, _ = make_collector(mt5)

    collector.poll_once(SYMBOL)
    collector.poll_once(SYMBOL)
    collector.poll_once(SYMBOL)

    assert mt5.range_calls == []


def test_changed_quote_at_the_same_broker_time_fills_same_millisecond_history():
    # Several prices can share one broker millisecond. If polling observes
    # only the first and last, the range read must still recover the middle
    # price because it may be the burst's high or low.
    mt5 = FakeMT5(
        info_ticks=[
            info_tick(T0_MSC, "158.840", "158.844"),
            info_tick(T0_MSC, "158.850", "158.854"),
        ],
        range_rows=[
            range_row(T0_MSC, "158.840", "158.844"),
            range_row(T0_MSC, "159.500", "159.504"),
            range_row(T0_MSC, "158.850", "158.854"),
        ],
    )
    collector, repository = make_collector(mt5)

    assert collector.poll_once(SYMBOL) == 1
    assert collector.poll_once(SYMBOL) == 2

    assert len(mt5.range_calls) == 1
    assert mt5.range_calls[0][1:3] == (T0, T0)
    assert [tick.bid for tick in repository.ticks] == [
        Decimal("158.840"),
        Decimal("159.500"),
        Decimal("158.850"),
    ]


def test_history_ticks_are_known_after_the_tick_history_fetch_returns():
    # The range read can take a while on a terminal that has to sync. Stamping
    # what it returns with the time from before the call would claim the
    # collector knew those prices earlier than it did, and a replay of the
    # span would then see the burst's extremes ahead of time.
    clock = FixedClock(T0)
    mt5 = FakeMT5(
        info_ticks=[
            info_tick(T0_MSC, "158.840", "158.844"),
            info_tick(T0_MSC + 200, "158.850", "158.854"),
        ],
        range_rows=[range_row(T0_MSC + 100, "159.500", "159.504")],
        on_range_call=lambda: clock.advance(seconds=1),
    )
    collector, repository = make_collector(mt5, clock)

    collector.poll_once(SYMBOL)
    collector.poll_once(SYMBOL)

    history_tick = repository.ticks[1]
    polled_tick = repository.ticks[2]
    assert history_tick.bid == Decimal("159.500")
    assert polled_tick.bid == Decimal("158.850")
    assert history_tick.known_time > polled_tick.known_time


def test_same_time_history_keeps_the_polled_quote_before_newer_quotes():
    # A quote can land between the two terminal calls, so the range read may
    # hold prices newer than the polled one under the same millisecond. Stored
    # ticks are read back in (event_time, id) order, so writing the polled
    # quote after them would hand the bar an older close.
    mt5 = FakeMT5(
        info_ticks=[
            info_tick(T0_MSC, "158.840", "158.844"),
            info_tick(T0_MSC, "158.850", "158.854"),
        ],
        range_rows=[
            range_row(T0_MSC, "158.840", "158.844"),
            range_row(T0_MSC, "158.850", "158.854"),
            range_row(T0_MSC, "159.500", "159.504"),
        ],
    )
    collector, repository = make_collector(mt5)

    collector.poll_once(SYMBOL)
    collector.poll_once(SYMBOL)

    assert [tick.bid for tick in repository.ticks] == [
        Decimal("158.840"),
        Decimal("158.850"),
        Decimal("159.500"),
    ]


def test_the_first_poll_of_a_process_fills_no_gap():
    # With no previous quote there is no near edge to read from, and
    # guessing one would make every restart re-import an arbitrary span.
    mt5 = FakeMT5(
        info_ticks=[info_tick(T0_MSC, "158.840", "158.844")],
        range_rows=[range_row(T0_MSC - 100, "158.830", "158.834")],
    )
    collector, repository = make_collector(mt5)

    assert collector.poll_once(SYMBOL) == 1

    assert mt5.range_calls == []
    assert len(repository.ticks) == 1


def test_a_gap_wider_than_the_bound_is_left_to_backfill():
    # A weekend or a collector outage is not an undersampled burst. Pulling
    # it through the poll loop would import a period at the poll rate; the
    # scheduled backfill exists for exactly that span.
    ten_minutes_later = T0_MSC + 10 * 60 * 1000
    mt5 = FakeMT5(
        info_ticks=[
            info_tick(T0_MSC, "158.840", "158.844"),
            info_tick(ten_minutes_later, "158.900", "158.904"),
        ],
        range_rows=[range_row(T0_MSC + 100, "159.500", "159.504")],
    )
    collector, repository = make_collector(mt5)

    collector.poll_once(SYMBOL)
    assert collector.poll_once(SYMBOL) == 1

    assert mt5.range_calls == []
    assert [tick.bid for tick in repository.ticks] == [
        Decimal("158.840"),
        Decimal("158.900"),
    ]


def test_a_failed_tick_history_fetch_is_not_read_as_an_empty_burst():
    # The same rule the quote fetch follows: an outage that reads as "no
    # quotes in the hole" would leave the gap silently unrepaired.
    mt5 = FakeMT5(
        info_ticks=[
            info_tick(T0_MSC, "158.840", "158.844"),
            info_tick(T0_MSC + 1000, "158.900", "158.904"),
        ],
        range_rows=None,
    )
    collector, repository = make_collector(mt5)

    collector.poll_once(SYMBOL)
    with pytest.raises(MT5ConnectionError):
        collector.poll_once(SYMBOL)

    assert len(mt5.range_calls) == 1
    assert len(repository.ticks) == 1


def test_backfill_requests_the_given_range_and_stores_its_ticks():
    clock = FixedClock(T0)
    start = T0 - timedelta(hours=2)
    end = T0
    rows = [
        range_row(int((start + timedelta(minutes=1)).timestamp() * 1000), "158.840", "158.844"),
        range_row(int((start + timedelta(minutes=2)).timestamp() * 1000), "158.845", "158.849"),
    ]
    mt5 = FakeMT5(range_rows=rows)
    collector, repository = make_collector(mt5, clock)

    assert collector.backfill(SYMBOL, start, end) == 2

    assert len(mt5.range_calls) == 1
    symbol, date_from, date_to, _flags = mt5.range_calls[0]
    assert (symbol, date_from, date_to) == (SYMBOL, start, end)
    assert date_from.tzinfo is not None and date_to.tzinfo is not None
    assert [t.bid for t in repository.ticks] == [Decimal("158.840"), Decimal("158.845")]
    # Backfilled quotes became known when they were read, not when the broker
    # timestamped them.
    assert {t.received_at for t in repository.ticks} == {clock.now()}


def test_backfill_failure_raises_but_an_empty_range_is_normal():
    collector, _ = make_collector(FakeMT5(range_rows=None))
    with pytest.raises(MT5ConnectionError):
        collector.backfill(SYMBOL, T0 - timedelta(hours=1), T0)

    empty, repository = make_collector(FakeMT5(range_rows=[]))
    assert empty.backfill(SYMBOL, T0 - timedelta(hours=1), T0) == 0
    assert repository.ticks == []


def test_connect_selects_the_symbol_and_surfaces_failures():
    mt5 = FakeMT5()
    collector, _ = make_collector(mt5)

    collector.connect(SYMBOL)
    # An unselected symbol yields no tick at all, so selection is not optional.
    assert mt5.selected == [(SYMBOL, True)]

    failed_init, _ = make_collector(FakeMT5(initialize_ok=False))
    with pytest.raises(MT5ConnectionError):
        failed_init.connect(SYMBOL)

    failed_select, _ = make_collector(FakeMT5(select_ok=False))
    with pytest.raises(MT5ConnectionError):
        failed_select.connect(SYMBOL)


def test_backfill_converts_and_writes_in_chunks(monkeypatch):
    # A busy day returns ~10^6 rows. Converting the window in one pass would
    # hold every Tick object on top of the array the terminal returned, so the
    # rows themselves are sliced before any Tick is built.
    monkeypatch.setattr(collector_module, "INSERT_CHUNK_SIZE", 2)
    start = T0 - timedelta(hours=1)
    rows = [
        range_row(int((start + timedelta(minutes=i)).timestamp() * 1000), "158.840", "158.844")
        for i in range(5)
    ]
    collector, repository = make_collector(FakeMT5(range_rows=rows))

    assert collector.backfill(SYMBOL, start, T0) == 5
    assert [len(batch) for batch in repository.batches] == [2, 2, 1]


def test_long_backfill_is_split_into_windows():
    start = T0 - timedelta(days=3)
    mt5 = FakeMT5(range_rows=[])
    collector, _ = make_collector(mt5)

    collector.backfill(SYMBOL, start, T0)

    assert len(mt5.range_calls) == 3
    boundaries = [(call[1], call[2]) for call in mt5.range_calls]
    assert boundaries[0][0] == start
    assert boundaries[-1][1] == T0
    # Contiguous, so no period falls between two windows. A tick landing
    # exactly on a boundary may be fetched by both and is dropped by the
    # unique key; a gap would be lost data and could not be.
    for (_, previous_end), (next_start, _) in pairwise(boundaries):
        assert previous_end == next_start


def test_every_write_carries_the_same_run_provenance():
    collector, repository = make_collector(
        FakeMT5(
            info_ticks=[
                info_tick(T0_MSC, "158.840", "158.844"),
                info_tick(T0_MSC + 500, "158.845", "158.849"),
            ]
        )
    )

    collector.poll_once(SYMBOL)
    collector.poll_once(SYMBOL)

    # Both columns are NOT NULL, and one process is one run.
    assert {call["source"] for call in repository.calls} == {"MT5"}
    assert len({call["ingestion_run"] for call in repository.calls}) == 1
