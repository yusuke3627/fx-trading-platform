"""Tick collector: broker fetch failures must never read as an empty feed,
and reception time must be stamped by the injected clock so the stored series
stays point-in-time."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from types import SimpleNamespace

import pytest

from tests.support import FixedClock
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
        range_rows=None,
        initialize_ok: bool = True,
        select_ok: bool = True,
    ) -> None:
        self._info_ticks = list(info_ticks)
        self._range_rows = range_rows
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
        if self._range_rows is None:
            return None
        return [r for r in self._range_rows if date_from <= _row_time(r) < date_to]

    def last_error(self):
        return (-10004, "no connection")


def _row_time(row: dict) -> datetime:
    return datetime.fromtimestamp(row["time_msc"] / 1000, tz=UTC)


class FakeTickRepository:
    def __init__(self) -> None:
        self.ticks = []
        self.calls: list[dict] = []

    def insert_many(self, ticks, *, source, ingestion_run) -> int:
        self.ticks.extend(ticks)
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
