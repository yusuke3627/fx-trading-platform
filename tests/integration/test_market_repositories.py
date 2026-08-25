"""PostgreSQL-backed visibility semantics of market_ticks / market_bars.

Requires TRADING_DB_DSN (see tests/integration/README.md); skipped without it.
Each test uses a throwaway symbol and removes its own rows.

These exercise the SQL itself — column lists, ordering, tie-breaks — which the
unit-level fakes cannot check: a fake answers from objects it was handed, so a
column missing from a SELECT only ever surfaces against a real database.
"""
from __future__ import annotations

import os
from datetime import timedelta
from uuid import uuid4

import pytest

from tests.support import T0, at, make_bar, make_tick

DSN = os.environ.get("TRADING_DB_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")

BROKER_OFFSET = timedelta(hours=3)


@pytest.fixture
def repos():
    from trading.storage.postgres import (
        PostgresMarketBarRepository,
        PostgresMarketTickRepository,
        connect,
    )

    conn = connect(DSN)
    symbol = f"TEST{uuid4().hex[:8].upper()}"
    yield PostgresMarketTickRepository(conn), PostgresMarketBarRepository(conn), symbol
    conn.execute("DELETE FROM market_ticks WHERE symbol = %s", (symbol,))
    conn.execute("DELETE FROM market_bars WHERE symbol = %s", (symbol,))
    conn.commit()
    conn.close()


def store(ticks_repo, ticks) -> int:
    return ticks_repo.insert_many(ticks, source="TEST", ingestion_run=uuid4())


def test_latest_follows_broker_time_not_arrival(repos):
    ticks, _, symbol = repos
    store(
        ticks,
        [
            make_tick("158.850", "158.854", time=at(minutes=4), symbol=symbol),
            # A reconnect delivers an older quote after a newer one.
            make_tick(
                "158.800",
                "158.804",
                time=at(minutes=0),
                received_at=at(minutes=5),
                symbol=symbol,
            ),
        ],
    )

    latest = ticks.latest_known_before(symbol, at(minutes=10))

    assert latest is not None and latest.time == at(minutes=4)


def test_latest_is_hidden_until_the_quote_is_received(repos):
    ticks, _, symbol = repos
    store(
        ticks,
        [
            make_tick(
                "158.840",
                "158.844",
                time=at(minutes=1),
                received_at=at(minutes=5),
                symbol=symbol,
            )
        ],
    )

    assert ticks.latest_known_before(symbol, at(minutes=2)) is None
    assert ticks.latest_known_before(symbol, at(minutes=6)) is not None


def test_latest_agrees_with_the_last_row_of_the_window(repos):
    # Quotes sharing an event_time are distinct rows (the key includes
    # bid/ask), so which one counts as "the price" is decided by the tie-break.
    # The window query and the latest query must not disagree about it.
    ticks, _, symbol = repos
    store(
        ticks,
        [
            make_tick("158.840", "158.844", time=at(minutes=3), symbol=symbol),
            make_tick("158.841", "158.845", time=at(minutes=3), symbol=symbol),
            make_tick("158.842", "158.846", time=at(minutes=3), symbol=symbol),
        ],
    )

    window = ticks.known_before(symbol, at(minutes=10), T0)
    latest = ticks.latest_known_before(symbol, at(minutes=10))

    assert len(window) == 3
    assert latest == window[-1]


def test_latest_is_none_for_a_symbol_with_no_quotes(repos):
    ticks, _, symbol = repos

    assert ticks.latest_known_before(symbol, at(minutes=10)) is None


def test_the_window_starts_at_since(repos):
    ticks, _, symbol = repos
    store(
        ticks,
        [
            make_tick("158.840", "158.844", time=at(minutes=minute), symbol=symbol)
            for minute in (0, 5, 9)
        ],
    )

    window = ticks.known_before(symbol, at(minutes=10), at(minutes=5))

    assert [t.time for t in window] == [at(minutes=5), at(minutes=9)]


def test_earliest_after_is_the_first_row_of_the_window(repos):
    ticks, _, symbol = repos
    store(
        ticks,
        [
            make_tick(
                "158.840",
                "158.844",
                time=at(minutes=minute),
                received_at=at(minutes=minute),
                symbol=symbol,
            )
            for minute in (0, 5, 9)
        ],
    )

    at_ten = at(minutes=10)
    assert ticks.earliest_known_after(symbol, at_ten, at(minutes=5)).time == at(minutes=5)
    # Both bounds still apply: a quote before `since`, and one that has not
    # been received yet, are equally invisible.
    assert ticks.earliest_known_after(symbol, at_ten, at(minutes=10)) is None
    assert ticks.earliest_known_after(symbol, at(minutes=1), at(minutes=5)) is None

    window = ticks.known_before(symbol, at_ten, at(minutes=5))
    assert ticks.earliest_known_after(symbol, at_ten, at(minutes=5)) == window[0]


def test_the_same_quote_twice_is_stored_once(repos):
    # Re-ingesting a range is the normal way to fill a gap, so insert_many
    # reports what it actually added rather than what it was handed.
    ticks, _, symbol = repos
    quote = make_tick("158.840", "158.844", time=at(minutes=1), symbol=symbol)

    assert store(ticks, [quote]) == 1
    assert store(ticks, [quote]) == 0


def test_a_different_price_at_the_same_instant_is_kept(repos):
    # The uniqueness key includes bid/ask: a repeated event_time carrying a
    # different price is a genuine second quote within that second, not a
    # duplicate to discard.
    ticks, _, symbol = repos

    first = make_tick("158.840", "158.844", time=at(minutes=1), symbol=symbol)
    second = make_tick("158.841", "158.845", time=at(minutes=1), symbol=symbol)

    assert store(ticks, [first]) == 1
    assert store(ticks, [second]) == 1


def test_between_reads_the_period_regardless_of_reception(repos):
    # The research read (ADR-007): a backfilled row's received_at lies far in
    # the tick's future and must not hide it; the bounds are event_time only,
    # end-exclusive, in the same (event_time, id) order the visibility reads
    # use.
    ticks, _, symbol = repos
    store(
        ticks,
        [
            make_tick(
                "158.840",
                "158.844",
                time=at(minutes=minute),
                received_at=at(days=100),
                symbol=symbol,
            )
            for minute in (0, 5, 9)
        ],
    )

    window = ticks.between(symbol, at(minutes=0), at(minutes=9))

    assert [t.time for t in window] == [at(minutes=0), at(minutes=5)]


def test_a_bar_round_trips_through_the_database(repos):
    _, bars, symbol = repos
    # ADR-005: the candle sits on the broker's clock, known_at on ours, so
    # known_at is BEFORE end_at whenever the broker runs ahead.
    written = make_bar(
        "158.80",
        "158.90",
        "158.70",
        "158.85",
        start=at(hours=1) + BROKER_OFFSET,
        symbol=symbol,
        timeframe="1h",
        tick_volume=42,
        known_at=at(hours=2),
    )
    assert bars.insert_many([written]) == 1

    (read,) = bars.known_before(symbol, "1h", at(hours=3), 10)

    assert read == written


def test_bars_are_invisible_before_their_known_at(repos):
    _, bars, symbol = repos
    bars.insert_many(
        [
            make_bar(
                "158.80",
                "158.90",
                "158.70",
                "158.85",
                start=at(hours=hour),
                symbol=symbol,
                timeframe="1h",
            )
            for hour in range(3)
        ]
    )

    assert bars.known_before(symbol, "1h", T0, 10) == []
    assert len(bars.known_before(symbol, "1h", at(hours=2), 10)) == 2


def test_bars_returns_the_most_recent_count_oldest_first(repos):
    _, bars, symbol = repos
    bars.insert_many(
        [
            make_bar(
                "158.80",
                "158.90",
                "158.70",
                "158.85",
                start=at(hours=hour),
                symbol=symbol,
                timeframe="1h",
            )
            for hour in range(3)
        ]
    )

    recent = bars.known_before(symbol, "1h", at(hours=5), 2)

    assert [b.start for b in recent] == [at(hours=1), at(hours=2)]
