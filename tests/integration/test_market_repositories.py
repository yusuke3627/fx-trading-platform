"""PostgreSQL-backed visibility semantics of market_ticks / market_bars.

Requires TRADING_DB_DSN (see tests/integration/README.md); skipped without it.
Each test uses a throwaway symbol and removes its own rows.

These exercise the SQL itself — column lists, ordering, tie-breaks — which the
unit-level fakes cannot check: a fake answers from objects it was handed, so a
column missing from a SELECT only ever surfaces against a real database.
"""
from __future__ import annotations

import os
import threading
import time
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


def waits_for_tick_lock(observer, pid: int) -> bool:
    from trading.storage.postgres import _TICK_ADVISORY_LOCK_CLASS_ID

    row = observer.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE pid = %s AND locktype = 'advisory'
              AND classid = %s::oid AND objsubid = 2 AND NOT granted
        ) AS waiting
        """,
        (pid, _TICK_ADVISORY_LOCK_CLASS_ID),
    ).fetchone()
    observer.commit()
    return row["waiting"]


def waits_for_the_tick_table(observer, pid: int) -> bool:
    row = observer.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE pid = %s AND locktype = 'relation'
              AND relation = 'market_ticks'::regclass AND NOT granted
        ) AS waiting
        """,
        (pid,),
    ).fetchone()
    observer.commit()
    return row["waiting"]


def wait_until(check, finished: threading.Event, what: str) -> None:
    deadline = time.monotonic() + 5
    while not check():
        assert not finished.is_set(), f"operation finished without waiting for {what}"
        assert time.monotonic() < deadline, f"the wait for {what} was not observed"
        finished.wait(0.01)


def wait_for_tick_lock(observer, pid: int, finished: threading.Event) -> None:
    wait_until(lambda: waits_for_tick_lock(observer, pid), finished, "the tick lock")


def insert_in_thread(quotes, started, finished, pids, counts, errors) -> None:
    from trading.storage import postgres

    try:
        with postgres.connect(DSN) as writer:
            pids.append(writer.info.backend_pid)
            started.set()
            counts.append(store(postgres.PostgresMarketTickRepository(writer), quotes))
    except postgres.psycopg.Error as exc:
        errors.append(exc)
    finally:
        finished.set()


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
    # A single-row range answers with that row as both edges.
    single = ticks.bounds_between(symbol, at(minutes=5), at(minutes=6))
    assert single is not None and single[0] == single[1]


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


def test_stream_waits_for_a_writer_that_began_after_the_readers_transaction(repos, monkeypatch):
    # The reader connection often has an open transaction from earlier PIT
    # reads. A writer that starts later must still be waited on, regardless
    # of that earlier transaction's start time.
    from trading.storage import postgres

    monkeypatch.setattr(postgres, "_STREAM_SETTLE_TIMEOUT_SECONDS", 0.5)
    ticks, _, symbol = repos
    store(ticks, [make_tick("158.840", "158.844", time=at(minutes=0), symbol=symbol)])
    # Open a transaction on the reader connection well before the writer.
    ticks.latest_known_before(symbol, at(minutes=10))

    with postgres.connect(DSN) as writer:
        writer.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (postgres._TICK_ADVISORY_LOCK_CLASS_ID, symbol),
        )
        writer.execute(
            """
            INSERT INTO market_ticks (
                symbol, bid, ask, event_time, received_at, source, ingestion_run
            ) VALUES (%s, %s, %s, %s, %s, 'TEST', %s)
            """,
            (symbol, "158.860", "158.864", at(minutes=2), at(minutes=2), uuid4()),
        )
        with pytest.raises(RuntimeError, match=symbol) as raised:
            next(ticks.stream_between(symbol, at(minutes=0), at(minutes=60)))
        assert raised.value.__cause__.sqlstate == "55P03"
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
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (postgres._TICK_ADVISORY_LOCK_CLASS_ID, symbol),
        )
        writer.execute(
            """
            INSERT INTO market_ticks (
                symbol, bid, ask, event_time, received_at, source, ingestion_run
            ) VALUES (%s, %s, %s, %s, %s, 'TEST', %s)
            """,
            (symbol, "158.850", "158.854", at(minutes=1), at(minutes=1), uuid4()),
        )
        # Deliberately no commit: the writer is mid-transaction.
        with pytest.raises(RuntimeError, match=symbol) as raised:
            next(ticks.stream_between(symbol, at(minutes=0), at(minutes=60)))
        assert "0.5s" in str(raised.value)
        assert isinstance(raised.value.__cause__, postgres.psycopg.errors.LockNotAvailable)
        assert raised.value.__cause__.sqlstate == "55P03"
        writer.rollback()

    assert [t.time for t in ticks.stream_between(symbol, at(minutes=0), at(minutes=60))] == [
        at(minutes=0)
    ]


def test_stream_reads_the_ceiling_before_taking_the_write_lock(repos, monkeypatch):
    # The order is the whole safeguard. Taking the key first and releasing it
    # before reading the ceiling would let a writer that starts in between
    # allocate an id below the ceiling and stay uncommitted inside the pin.
    # With the ceiling read blocked by an exclusive table lock, a reader that
    # still follows the order is waiting on the table and has not asked for
    # the key yet — a reader that asked first would be queued behind the
    # holder below instead.
    from trading.storage import postgres

    monkeypatch.setattr(postgres, "_STREAM_SETTLE_TIMEOUT_SECONDS", 0.5)
    ticks, _, symbol = repos
    store(ticks, [make_tick("158.840", "158.844", time=at(minutes=0), symbol=symbol)])
    started, finished = threading.Event(), threading.Event()
    pids, errors = [], []

    def read():
        try:
            with postgres.connect(DSN) as reader:
                pids.append(reader.info.backend_pid)
                started.set()
                list(postgres.PostgresMarketTickRepository(reader).stream_between(
                    symbol, at(minutes=0), at(minutes=60)
                ))
        except (postgres.psycopg.Error, RuntimeError) as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=read)
    with (
        postgres.connect(DSN) as holder,
        postgres.connect(DSN) as blocker,
        postgres.connect(DSN) as observer,
    ):
        holder.execute(
            "SELECT pg_advisory_lock(%s, hashtext(%s))",
            (postgres._TICK_ADVISORY_LOCK_CLASS_ID, symbol),
        )
        holder.commit()
        try:
            blocker.execute("LOCK TABLE market_ticks IN ACCESS EXCLUSIVE MODE")
            worker.start()
            assert started.wait(5)
            wait_until(
                lambda: waits_for_the_tick_table(observer, pids[0]),
                finished,
                "the ceiling read",
            )
            assert not waits_for_tick_lock(observer, pids[0])
        finally:
            blocker.rollback()
            # Once the ceiling read goes through, the key is asked for and the
            # held key turns into the usual timeout.
            assert finished.wait(5)
            worker.join(timeout=5)
            holder.execute(
                "SELECT pg_advisory_unlock(%s, hashtext(%s))",
                (postgres._TICK_ADVISORY_LOCK_CLASS_ID, symbol),
            )
            holder.commit()
    assert [type(error) for error in errors] == [RuntimeError]


def test_stream_does_not_wait_for_another_symbols_writer(repos, monkeypatch):
    from trading.storage import postgres

    monkeypatch.setattr(postgres, "_STREAM_SETTLE_TIMEOUT_SECONDS", 0.5)
    ticks, _, symbol = repos
    other_symbol = f"TEST{uuid4().hex[:8].upper()}"
    quotes = [
        make_tick(
            "158.840", "158.844", time=at(minutes=m), received_at=at(minutes=m), symbol=symbol
        )
        for m in (0, 2)
    ]
    store(ticks, quotes)
    finished = threading.Event()
    seen, errors = [], []

    def read():
        try:
            with postgres.connect(DSN) as reader:
                seen.extend(postgres.PostgresMarketTickRepository(reader).stream_between(
                    symbol, at(minutes=0), at(minutes=60)
                ))
        except (postgres.psycopg.Error, RuntimeError) as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=read)
    with postgres.connect(DSN) as writer:
        try:
            writer.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (postgres._TICK_ADVISORY_LOCK_CLASS_ID, other_symbol),
            )
            writer.execute(
                """
                INSERT INTO market_ticks (
                    symbol, bid, ask, event_time, received_at, source, ingestion_run
                ) VALUES (%s, %s, %s, %s, %s, 'TEST', %s)
                """,
                (other_symbol, "158.850", "158.854", at(minutes=1), at(minutes=1), uuid4()),
            )
            worker.start()
            # The writer keeps its key and uncommitted row until the reader
            # has returned the full set, not merely its first page.
            assert finished.wait(5)
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert errors == []
            assert seen == quotes
        finally:
            writer.rollback()  # Also removes the extra symbol's pending row.
            if worker.ident is not None:
                worker.join(timeout=5)


def test_stream_returns_a_row_committed_below_the_ceiling(repos):
    from trading.storage import postgres

    ticks, _, symbol = repos
    other_symbol = f"TEST{uuid4().hex[:8].upper()}"
    first = make_tick(
        "158.840", "158.844", time=at(minutes=0), received_at=at(minutes=0), symbol=symbol
    )
    pending = make_tick(
        "158.850", "158.854", time=at(minutes=1), received_at=at(minutes=1), symbol=symbol
    )
    store(ticks, [first])
    inserted, release_writer = threading.Event(), threading.Event()
    reader_started, reader_finished = threading.Event(), threading.Event()
    inserted_ids, reader_pids, seen, errors = [], [], [], []

    def write():
        try:
            with postgres.connect(DSN) as writer:
                writer.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (postgres._TICK_ADVISORY_LOCK_CLASS_ID, symbol),
                )
                row = writer.execute(
                    """
                    INSERT INTO market_ticks (
                        symbol, bid, ask, event_time, received_at, source, ingestion_run
                    ) VALUES (%s, %s, %s, %s, %s, 'TEST', %s) RETURNING id
                    """,
                    (symbol, pending.bid, pending.ask, pending.time, pending.known_time, uuid4()),
                ).fetchone()
                inserted_ids.append(row["id"])
                inserted.set()
                assert release_writer.wait(10)
                # Keep the commit delayed after the lock wait was observed;
                # correctness is checked by the lock state and returned rows.
                time.sleep(0.5)
                writer.commit()
        except (postgres.psycopg.Error, AssertionError) as exc:
            errors.append(exc)

    def read():
        try:
            with postgres.connect(DSN) as reader:
                reader_pids.append(reader.info.backend_pid)
                reader_started.set()
                seen.extend(postgres.PostgresMarketTickRepository(reader).stream_between(
                    symbol, at(minutes=0), at(minutes=60)
                ))
        except (postgres.psycopg.Error, RuntimeError) as exc:
            errors.append(exc)
        finally:
            reader_finished.set()

    writer_thread = threading.Thread(target=write)
    reader_thread = threading.Thread(target=read)
    writer_thread.start()
    try:
        assert inserted.wait(5)
        store(ticks, [make_tick("158.860", "158.864", time=at(minutes=2), symbol=other_symbol)])
        with postgres.connect(DSN) as observer:
            ceiling = observer.execute("SELECT max(id) AS ceiling FROM market_ticks").fetchone()
            observer.commit()
            assert ceiling["ceiling"] > inserted_ids[0]
            reader_thread.start()
            assert reader_started.wait(5)
            wait_for_tick_lock(observer, reader_pids[0], reader_finished)
            assert not reader_finished.is_set()
            release_writer.set()
            writer_thread.join(timeout=5)
            reader_thread.join(timeout=5)
            assert not writer_thread.is_alive()
            assert not reader_thread.is_alive()
            assert errors == []
            assert seen == [first, pending]
    finally:
        release_writer.set()
        writer_thread.join(timeout=5)
        if reader_thread.ident is not None:
            reader_thread.join(timeout=5)
        with postgres.connect(DSN) as cleanup:
            cleanup.execute("DELETE FROM market_ticks WHERE symbol = %s", (other_symbol,))


@pytest.mark.parametrize("hold_same_symbol", [True, False], ids=["same-symbol", "other-symbol"])
def test_insert_many_waits_for_the_symbol_write_lock(repos, hold_same_symbol):
    from trading.storage import postgres

    ticks, _, symbol = repos
    other_symbol = f"TEST{uuid4().hex[:8].upper()}"
    held_symbol = symbol if hold_same_symbol else other_symbol
    # Initialize the sequence so last_value has an allocated value to compare.
    store(ticks, [make_tick("158.840", "158.844", time=at(minutes=0), symbol=symbol)])
    quotes = [make_tick(
        "158.850", "158.854", time=at(minutes=1), received_at=at(minutes=1), symbol=symbol
    )]
    started, finished = threading.Event(), threading.Event()
    pids, counts, errors = [], [], []
    worker = threading.Thread(
        target=insert_in_thread, args=(quotes, started, finished, pids, counts, errors)
    )
    with postgres.connect(DSN) as holder, postgres.connect(DSN) as observer:
        holder.execute(
            "SELECT pg_advisory_lock(%s, hashtext(%s))",
            (postgres._TICK_ADVISORY_LOCK_CLASS_ID, held_symbol),
        )
        holder.commit()
        try:
            before = observer.execute("SELECT last_value FROM market_ticks_id_seq").fetchone()
            observer.commit()
            worker.start()
            assert started.wait(5)
            if hold_same_symbol:
                wait_for_tick_lock(observer, pids[0], finished)
                assert not finished.is_set()
                during = observer.execute("SELECT last_value FROM market_ticks_id_seq").fetchone()
                observer.commit()
                assert during == before
            else:
                assert finished.wait(5)
                worker.join(timeout=5)
                assert not worker.is_alive()
                assert errors == []
                assert counts == [1]
        finally:
            holder.execute(
                "SELECT pg_advisory_unlock(%s, hashtext(%s))",
                (postgres._TICK_ADVISORY_LOCK_CLASS_ID, held_symbol),
            )
            holder.commit()
            if worker.ident is not None:
                worker.join(timeout=5)
        assert not worker.is_alive()
        assert finished.is_set()
        assert errors == []
        assert counts == [1]
    assert list(ticks.between(symbol, at(minutes=1), at(minutes=2))) == quotes


@pytest.mark.parametrize("held_index", [0, 1], ids=["first-symbol", "second-symbol"])
def test_insert_many_takes_every_symbols_lock_in_the_batch(repos, held_index):
    from trading.storage import postgres

    ticks, _, symbol = repos
    other_symbol = f"TEST{uuid4().hex[:8].upper()}"
    symbols = [symbol, other_symbol]
    quotes = [
        make_tick(
            "158.840", "158.844", time=at(minutes=0), received_at=at(minutes=0), symbol=s
        )
        for s in symbols
    ]
    started, finished = threading.Event(), threading.Event()
    pids, counts, errors = [], [], []
    worker = threading.Thread(
        target=insert_in_thread, args=(quotes, started, finished, pids, counts, errors)
    )
    with postgres.connect(DSN) as holder, postgres.connect(DSN) as observer:
        holder.execute(
            "SELECT pg_advisory_lock(%s, hashtext(%s))",
            (postgres._TICK_ADVISORY_LOCK_CLASS_ID, symbols[held_index]),
        )
        holder.commit()
        try:
            worker.start()
            assert started.wait(5)
            wait_for_tick_lock(observer, pids[0], finished)
            assert not finished.is_set()
        finally:
            holder.execute(
                "SELECT pg_advisory_unlock(%s, hashtext(%s))",
                (postgres._TICK_ADVISORY_LOCK_CLASS_ID, symbols[held_index]),
            )
            holder.commit()
            if worker.ident is not None:
                worker.join(timeout=5)
            # Only the fixture's original symbol is cleaned by repos.
            observer.execute("DELETE FROM market_ticks WHERE symbol = %s", (other_symbol,))
            observer.commit()
        assert not worker.is_alive()
        assert finished.is_set()
        assert errors == []
        assert counts == [2]
    assert list(ticks.between(symbol, at(minutes=0), at(minutes=1))) == [quotes[0]]


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
