"""Dukascopy tick インポーターのデコード、重複回避、再開、再試行を検証する。"""

import lzma
import struct
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.client import IncompleteRead
from urllib.error import URLError

import pytest

from tests.support import FixedClock
from trading.data.market.dukascopy import (
    REQUEST_INTERVAL_SECONDS,
    RETRY_WAIT_SECONDS,
    DukascopyTickImporter,
    decode_bi5,
    hour_url,
)
from trading.domain.market import Tick

SYMBOL = "USDJPY"
T0 = datetime(2024, 7, 11, 12, 0, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC)


def tick_at(when: datetime) -> Tick:
    return Tick(
        symbol=SYMBOL,
        bid=Decimal("150.001"),
        ask=Decimal("150.004"),
        time=when,
        received_at=when,
    )


def bi5_payload(*records: tuple[int, int, int]) -> bytes:
    raw = b"".join(
        struct.pack(">IIIff", msec, ask_point, bid_point, 1.0, 1.0)
        for msec, ask_point, bid_point in records
    )
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


class FakeTickRepository:
    def __init__(self, ticks: list[Tick] | None = None) -> None:
        self.ticks = list(ticks or [])
        self.batches: list[list[Tick]] = []
        self.calls: list[dict] = []

    def insert_many(self, ticks, *, source, ingestion_run) -> int:
        batch = list(ticks)
        self.ticks.extend(batch)
        self.batches.append(batch)
        self.calls.append({"source": source, "ingestion_run": ingestion_run})
        return len(batch)

    def bounds_between(
        self, symbol: str, start: datetime, end: datetime
    ) -> tuple[Tick, Tick] | None:
        matches = sorted(
            (tick for tick in self.ticks if tick.symbol == symbol and start <= tick.time < end),
            key=lambda tick: tick.time,
        )
        if not matches:
            return None
        return matches[0], matches[-1]


class FakeFetch:
    def __init__(self, payloads: Mapping[str, bytes | None]) -> None:
        self._payloads = payloads
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes | None:
        self.calls.append(url)
        return self._payloads.get(url)


def make_importer(
    repository: FakeTickRepository,
    fetch,
    *,
    sleep=lambda _seconds: None,
) -> DukascopyTickImporter:
    return DukascopyTickImporter(
        repository,
        fetch=fetch,
        clock=FixedClock(RECEIVED_AT),
        sleep=sleep,
    )


def hourly_payloads(start: datetime, hours: int) -> dict[str, bytes]:
    return {
        hour_url(SYMBOL, start + timedelta(hours=offset)): bi5_payload(
            (0, 150004 + offset, 150001 + offset)
        )
        for offset in range(hours)
    }


def test_hour_url_uses_zero_based_month() -> None:
    assert hour_url(SYMBOL, T0) == (
        "https://datafeed.dukascopy.com/datafeed/USDJPY/2024/06/11/12h_ticks.bi5"
    )
    assert hour_url(SYMBOL, datetime(2026, 1, 23, tzinfo=UTC)) == (
        "https://datafeed.dukascopy.com/datafeed/USDJPY/2026/00/23/00h_ticks.bi5"
    )


def test_decode_bi5_scales_points_without_float_conversion() -> None:
    clock = FixedClock(RECEIVED_AT)

    ticks = decode_bi5(
        bi5_payload((107, 161541, 161538)),
        SYMBOL,
        T0,
        clock.now(),
    )

    assert len(ticks) == 1
    assert ticks[0].bid == Decimal("161.538")
    assert ticks[0].ask == Decimal("161.541")
    assert ticks[0].time == T0 + timedelta(milliseconds=107)
    assert ticks[0].received_at == clock.now()


def test_decode_bi5_rejects_partial_record() -> None:
    payload = lzma.compress(b"partial-record", format=lzma.FORMAT_ALONE)

    with pytest.raises(ValueError, match="not divisible by 20"):
        decode_bi5(payload, SYMBOL, T0, RECEIVED_AT)


def test_empty_body_and_not_found_do_not_insert_or_fail() -> None:
    repository = FakeTickRepository()
    fetch = FakeFetch(
        {
            hour_url(SYMBOL, T0): b"",
            hour_url(SYMBOL, T0 + timedelta(hours=1)): None,
        }
    )
    importer = make_importer(repository, fetch)

    result = importer.import_range(SYMBOL, T0, T0 + timedelta(hours=2))

    assert result == (0, 0)
    assert repository.calls == []
    assert fetch.calls == [
        hour_url(SYMBOL, T0),
        hour_url(SYMBOL, T0 + timedelta(hours=1)),
    ]


def test_existing_mt5_day_is_not_fetched_at_source_boundary() -> None:
    july_22 = datetime(2024, 7, 22, tzinfo=UTC)
    existing = [tick_at(july_22 + timedelta(days=1, hours=hour, minutes=30)) for hour in range(24)]
    repository = FakeTickRepository(existing)
    fetch = FakeFetch(hourly_payloads(july_22, 24))
    importer = make_importer(repository, fetch)

    result = importer.import_range(SYMBOL, july_22, july_22 + timedelta(days=2))

    assert result == (24, 0)
    assert fetch.calls == [hour_url(SYMBOL, july_22 + timedelta(hours=hour)) for hour in range(24)]


def test_second_run_resumes_without_fetching_stored_hour() -> None:
    repository = FakeTickRepository()
    fetch = FakeFetch({hour_url(SYMBOL, T0): bi5_payload((0, 150004, 150001))})

    first = make_importer(repository, fetch).import_range(SYMBOL, T0, T0 + timedelta(hours=1))
    second = make_importer(repository, fetch).import_range(SYMBOL, T0, T0 + timedelta(hours=1))

    assert first == (1, 0)
    assert second == (0, 0)
    assert fetch.calls == [hour_url(SYMBOL, T0)]
    assert len(repository.calls) == 1


def test_partially_stored_day_fetches_only_missing_hours() -> None:
    day_start = datetime(2026, 1, 23, tzinfo=UTC)
    existing = [tick_at(day_start + timedelta(hours=hour, minutes=30)) for hour in range(12)]
    repository = FakeTickRepository(existing)
    fetch = FakeFetch(hourly_payloads(day_start + timedelta(hours=12), 12))
    importer = make_importer(repository, fetch)

    result = importer.import_range(SYMBOL, day_start, day_start + timedelta(days=1))

    expected_hours = [day_start + timedelta(hours=hour) for hour in range(12, 24)]
    assert result == (12, 0)
    assert fetch.calls == [hour_url(SYMBOL, hour) for hour in expected_hours]


def test_partial_start_skips_hour_with_existing_tick_before_since() -> None:
    since = T0 + timedelta(minutes=30)
    until = T0 + timedelta(hours=1)
    repository = FakeTickRepository([tick_at(T0 + timedelta(minutes=15))])
    fetch = FakeFetch({hour_url(SYMBOL, T0): bi5_payload((30 * 60 * 1000, 150004, 150001))})
    importer = make_importer(repository, fetch)

    result = importer.import_range(SYMBOL, since, until)

    assert result == (0, 0)
    assert fetch.calls == []
    assert repository.calls == []


def test_partial_end_skips_hour_with_existing_tick_after_until() -> None:
    since = T0
    until = T0 + timedelta(minutes=30)
    repository = FakeTickRepository([tick_at(T0 + timedelta(minutes=45))])
    fetch = FakeFetch({hour_url(SYMBOL, T0): bi5_payload((0, 150004, 150001))})
    importer = make_importer(repository, fetch)

    result = importer.import_range(SYMBOL, since, until)

    assert result == (0, 0)
    assert fetch.calls == []
    assert repository.calls == []


def test_transient_fetch_errors_are_retried() -> None:
    repository = FakeTickRepository()
    calls: list[str] = []
    sleeps: list[float] = []

    # IncompleteRead はボディ受信途中の切断で、OSError 系ではないが transient。
    errors = [URLError("temporary reset"), IncompleteRead(b"")]

    def fetch(url: str) -> bytes:
        calls.append(url)
        if errors:
            raise errors.pop(0)
        return bi5_payload((0, 150004, 150001))

    importer = make_importer(repository, fetch, sleep=sleeps.append)

    result = importer.import_range(SYMBOL, T0, T0 + timedelta(hours=1))

    assert result == (1, 0)
    assert calls == [hour_url(SYMBOL, T0)] * 3
    assert sleeps == [RETRY_WAIT_SECONDS, RETRY_WAIT_SECONDS, REQUEST_INTERVAL_SECONDS]


def test_exhausted_retries_are_counted_and_next_hour_continues(capsys) -> None:
    repository = FakeTickRepository()
    calls: list[str] = []
    first_url = hour_url(SYMBOL, T0)
    second_url = hour_url(SYMBOL, T0 + timedelta(hours=1))

    def fetch(url: str) -> bytes:
        calls.append(url)
        if url == first_url:
            raise URLError("still unavailable")
        return bi5_payload((0, 150004, 150001))

    importer = make_importer(repository, fetch)

    result = importer.import_range(SYMBOL, T0, T0 + timedelta(hours=2))

    assert result == (1, 1)
    assert calls == [first_url, first_url, first_url, second_url]
    assert "fetch failed after 3 attempts" in capsys.readouterr().err


def test_ticks_outside_requested_half_open_range_are_filtered() -> None:
    since = T0 + timedelta(minutes=30)
    until = T0 + timedelta(minutes=45)
    payload = bi5_payload(
        (29 * 60 * 1000, 150004, 150001),
        (30 * 60 * 1000, 150005, 150002),
        (44 * 60 * 1000 + 59_999, 150006, 150003),
        (45 * 60 * 1000, 150007, 150004),
    )
    repository = FakeTickRepository()
    importer = make_importer(repository, FakeFetch({hour_url(SYMBOL, T0): payload}))

    result = importer.import_range(SYMBOL, since, until)

    assert result == (2, 0)
    assert [tick.time for tick in repository.ticks] == [
        T0 + timedelta(minutes=30),
        T0 + timedelta(minutes=44, seconds=59, milliseconds=999),
    ]


def test_every_insert_uses_dukascopy_and_one_run_id() -> None:
    repository = FakeTickRepository()
    fetch = FakeFetch(hourly_payloads(T0, 2))
    importer = make_importer(repository, fetch)

    result = importer.import_range(SYMBOL, T0, T0 + timedelta(hours=2))

    assert result == (2, 0)
    assert len(repository.calls) == 2
    assert {call["source"] for call in repository.calls} == {"DUKASCOPY"}
    assert len({call["ingestion_run"] for call in repository.calls}) == 1
