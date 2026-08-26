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
    # The research read (ADR-014): a backfilled row's received_at lies far in
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


def test_stream_and_bounds_agree_with_between(repos):
    # The streaming read and the edge read are the same query in other
    # shapes; disagreement would let a replay see rows its coverage check
    # (or its materialized twin) does not.
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

    window = ticks.between(symbol, at(minutes=0), at(minutes=10))
    assert list(ticks.stream_between(symbol, at(minutes=0), at(minutes=10))) == list(
        window
    )
    assert ticks.bounds_between(symbol, at(minutes=0), at(minutes=10)) == (
        window[0],
        window[-1],
    )
    assert ticks.bounds_between(symbol, at(minutes=20), at(minutes=30)) is None


def test_stream_is_pinned_to_the_rows_present_at_its_start(repos, monkeypatch):
    # A concurrent backfill must not add rows to a replay already streaming:
    # the manifest digest describes the dataset, and a mid-run insert would
    # make it one no later run can reproduce.
    from trading.storage import postgres

    monkeypatch.setattr(postgres, "_STREAM_BATCH_ROWS", 2)
    ticks, _, symbol = repos
    store(
        ticks,
        [
            make_tick("158.840", "158.844", time=at(minutes=minute), symbol=symbol)
            for minute in (0, 2, 4)
        ],
    )

    stream = ticks.stream_between(symbol, at(minutes=0), at(minutes=60))
    seen = [next(stream)]
    store(
        ticks,
        [make_tick("158.850", "158.854", time=at(minutes=6), symbol=symbol)],
    )
    seen.extend(stream)

    assert [t.time for t in seen] == [at(minutes=m) for m in (0, 2, 4)]


def test_stream_fails_loudly_when_unfetched_rows_are_deleted(repos, monkeypatch):
    # A delete in the not-yet-fetched range would otherwise just shrink the
    # replay: later batches see the post-delete snapshot and the run ends
    # looking complete. The settled count turns that into a hard failure.
    from trading.storage import postgres

    monkeypatch.setattr(postgres, "_STREAM_BATCH_ROWS", 2)
    ticks, _, symbol = repos
    store(
        ticks,
        [
            make_tick("158.840", "158.844", time=at(minutes=minute), symbol=symbol)
            for minute in (0, 2, 4)
        ],
    )

    stream = ticks.stream_between(symbol, at(minutes=0), at(minutes=60))
    seen = [next(stream)]
    with postgres.connect(DSN) as writer:
        writer.execute(
            "DELETE FROM market_ticks WHERE symbol = %s AND event_time = %s",
            (symbol, at(minutes=4)),
        )
        writer.commit()
    with pytest.raises(RuntimeError):
        seen.extend(stream)
    assert len(seen) == 2


def test_stream_fails_loudly_when_fetched_rows_are_deleted(repos, monkeypatch):
    # A delete of an ALREADY-streamed row leaves the replay itself intact but
    # the manifest describing a dataset that no longer exists; the closing
    # re-count turns that into a failure too.
    from trading.storage import postgres

    monkeypatch.setattr(postgres, "_STREAM_BATCH_ROWS", 2)
    ticks, _, symbol = repos
    store(
        ticks,
        [
            make_tick("158.840", "158.844", time=at(minutes=minute), symbol=symbol)
            for minute in (0, 2, 4)
        ],
    )

    stream = ticks.stream_between(symbol, at(minutes=0), at(minutes=60))
    seen = [next(stream), next(stream)]
    with postgres.connect(DSN) as writer:
        writer.execute(
            "DELETE FROM market_ticks WHERE symbol = %s AND event_time = %s",
            (symbol, at(minutes=0)),
        )
        writer.commit()
    with pytest.raises(RuntimeError):
        list(stream)
    assert len(seen) == 2


def test_stream_fails_loudly_when_rows_are_updated(repos, monkeypatch):
    # An UPDATE keeps the row count intact, so only the whole-row content
    # fingerprint can tell that the replayed data and the stored data have
    # diverged mid-run.
    from trading.storage import postgres

    monkeypatch.setattr(postgres, "_STREAM_BATCH_ROWS", 2)
    ticks, _, symbol = repos
    store(
        ticks,
        [
            make_tick("158.840", "158.844", time=at(minutes=minute), symbol=symbol)
            for minute in (0, 2, 4)
        ],
    )

    stream = ticks.stream_between(symbol, at(minutes=0), at(minutes=60))
    seen = [next(stream)]
    with postgres.connect(DSN) as writer:
        writer.execute(
            "UPDATE market_ticks SET bid = %s WHERE symbol = %s AND event_time = %s",
            ("158.900", symbol, at(minutes=4)),
        )
        writer.commit()
    with pytest.raises(RuntimeError):
        list(stream)
    assert len(seen) == 1


def test_pin_instant_is_the_ceiling_read_not_the_transaction_start(repos, monkeypatch):
    # The reader connection often has an open transaction from earlier PIT
    # reads. now() would freeze the pin at that transaction's start, letting
    # a writer that began in between escape the settle wait.
    from trading.storage import postgres

    monkeypatch.setattr(postgres, "_STREAM_SETTLE_TIMEOUT_SECONDS", 0.5)
    ticks, _, symbol = repos
    store(ticks, [make_tick("158.840", "158.844", time=at(minutes=0), symbol=symbol)])
    # Open a transaction on the reader connection well before the writer.
    ticks.latest_known_before(symbol, at(minutes=10))

    with postgres.connect(DSN) as writer:
        writer.execute(
            """
            INSERT INTO market_ticks (
                symbol, bid, ask, event_time, received_at, source, ingestion_run
            ) VALUES (%s, %s, %s, %s, %s, 'TEST', %s)
            """,
            (symbol, "158.860", "158.864", at(minutes=2), at(minutes=2), uuid4()),
        )
        with pytest.raises(RuntimeError):
            next(ticks.stream_between(symbol, at(minutes=0), at(minutes=60)))
        writer.rollback()


def test_stream_refuses_to_start_over_an_unsettled_write(repos, monkeypatch):
    # An insert can hold an id below the ceiling while uncommitted; streaming
    # anyway would let it surface to a later batch nondeterministically. The
    # start waits for such writers and fails loudly when one never finishes.
    from trading.storage import postgres

    monkeypatch.setattr(postgres, "_STREAM_SETTLE_TIMEOUT_SECONDS", 0.5)
    ticks, _, symbol = repos
    store(ticks, [make_tick("158.840", "158.844", time=at(minutes=0), symbol=symbol)])

    with postgres.connect(DSN) as writer:
        writer.execute(
            """
            INSERT INTO market_ticks (
                symbol, bid, ask, event_time, received_at, source, ingestion_run
            ) VALUES (%s, %s, %s, %s, %s, 'TEST', %s)
            """,
            (symbol, "158.850", "158.854", at(minutes=1), at(minutes=1), uuid4()),
        )
        # Deliberately no commit: the writer is mid-transaction.
        with pytest.raises(RuntimeError):
            next(ticks.stream_between(symbol, at(minutes=0), at(minutes=60)))
        writer.rollback()


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
