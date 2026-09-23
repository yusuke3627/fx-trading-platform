"""同期専用の使い捨て DB を作成し、接続元の DB 自体には書き込まない。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

DSN = os.environ.get("TRADING_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")


@pytest.fixture(scope="module")
def databases():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    names = []
    migrations = sorted((Path(__file__).resolve().parents[2] / "migrations").glob("*.sql"))
    # CREATE DATABASE / DROP DATABASE 以外は、ここで作った DB のみが対象。
    with psycopg.connect(DSN, autocommit=True, connect_timeout=3) as admin:
        try:
            for _ in range(2):
                name = f"test_research_mirror_{uuid4().hex}"
                admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(name)))
                names.append(name)
                with psycopg.connect(make_conninfo(DSN, dbname=name), autocommit=True) as conn:
                    for migration in migrations:
                        conn.execute(migration.read_text())
            yield tuple(make_conninfo(DSN, dbname=name) for name in names)
        finally:
            for name in reversed(names):
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def pair(databases):
    from psycopg import sql

    from trading.storage import research_mirror as mirror

    for dsn in databases:
        with mirror._connect(dsn) as conn:
            conn.execute("TRUNCATE public.market_ticks, public.macro_observations, "
                         "public.events, public.swap_snapshots RESTART IDENTITY CASCADE")
            database = conn.execute("SELECT current_database()").fetchone()[0]
            conn.execute(sql.SQL("COMMENT ON DATABASE {} IS NULL").format(sql.Identifier(database)))
    return databases


def tick(conn, symbol="USDJPY", *, second=0) -> int:
    return conn.execute(
        "INSERT INTO public.market_ticks "
        "(symbol,bid,ask,event_time,received_at,source,ingestion_run) "
        "VALUES (%s,150.01,150.02,'2026-01-01'::timestamptz + %s * interval '1 second', "
        "'2026-01-02','TEST',%s) RETURNING id", (symbol, second, uuid4()),
    ).fetchone()[0]


def small_rows(conn, *, suffix=""):
    # 子行を親行より先に置き、COPY が自己参照 FK を文末で検査することを確認する。
    parent, child = UUID(int=200), UUID(int=100)
    conn.execute(
        "INSERT INTO public.macro_observations "
        "(id,series,observation_period,value,revision_of,source,known_at) VALUES "
        "(%s,%s,'2026-01',2,%s,'TEST','2026-02-02'),"
        "(%s,%s,'2026-01',1,NULL,'TEST','2026-02-01')",
        (child, f"TEST{suffix}", parent, parent, f"TEST{suffix}"),
    )
    conn.execute(
        "INSERT INTO public.events (id,event_type,source,payload,retrieved_at,known_at) "
        "VALUES (%s,'TEST',%s,%s::jsonb,'2026-01-01','2026-01-01')",
        (uuid4(), f"架空\t複数行\n\\{suffix}", '{"test": [null, "日本語", 2]}'),
    )
    conn.execute(
        "INSERT INTO public.swap_snapshots "
        "(id,symbol,swap_mode,swap_long,swap_short,swap_rollover3days,retrieved_at,known_at) "
        "VALUES (%s,'USDJPY',1,-2.125,0.25,3,'2026-01-01','2026-01-01')", (uuid4(),),
    )


def rows(dsn, table):
    from psycopg import sql

    from trading.storage import research_mirror as mirror

    with mirror._connect(dsn) as conn:
        return conn.execute(sql.SQL("SELECT * FROM {} ORDER BY id").format(
            sql.Identifier("public", table))).fetchall()


def test_incremental_resume_preserves_ids_and_obeys_chunk_limit_and_sleep(pair, monkeypatch):
    from trading.storage import research_mirror as mirror

    source, target = pair
    with mirror._connect(source) as conn:
        ids = [tick(conn, second=0), tick(conn, "EURUSD", second=1),
               tick(conn, second=2), tick(conn, second=3), tick(conn, second=4)]
    mirror.initialize(target)
    sleeps = []
    monkeypatch.setattr(mirror.time, "sleep", sleeps.append)
    options = mirror.MirrorOptions(chunk_size=2, sleep_seconds=0.125, max_rows=2)
    first = mirror.synchronize(source, target, options)
    assert first.ceiling == ids[-1]
    assert first.rows["market_ticks"] == 2
    assert [row[0] for row in rows(target, "market_ticks")] == [ids[0], ids[2]]
    assert sleeps == [0.125]
    second = mirror.synchronize(source, target, options)
    assert second.rows["market_ticks"] == 2
    assert [row[0] for row in rows(target, "market_ticks")] == [ids[0], *ids[2:]]
    third = mirror.synchronize(source, target, mirror.MirrorOptions(symbols=("USDJPY", "EURUSD")))
    assert third.rows["market_ticks"] == 1
    assert rows(source, "market_ticks") == rows(target, "market_ticks")
    assert mirror.synchronize(source, target, options).rows["market_ticks"] == 0


def test_small_tables_replace_atomically_and_preserve_all_columns(pair):
    from trading.storage import research_mirror as mirror

    source, target = pair
    mirror.initialize(target)
    for dsn, suffix in ((source, "new"), (target, "old")):
        with mirror._connect(dsn) as conn:
            small_rows(conn, suffix=suffix)
    report = mirror.synchronize(source, target)
    assert report.rows == {"market_ticks": 0, "macro_observations": 2, "events": 1, "swap_snapshots": 1}
    for table in mirror.TABLES[1:]:
        assert rows(source, table) == rows(target, table)


def test_unmarked_destination_is_unchanged(pair):
    from trading.storage import research_mirror as mirror

    source, target = pair
    with mirror._connect(target) as conn:
        small_rows(conn, suffix="keep")
    before = {table: rows(target, table) for table in mirror.TABLES}
    with pytest.raises(mirror.MirrorError, match="印"):
        mirror.synchronize(source, target)
    assert before == {table: rows(target, table) for table in mirror.TABLES}


@pytest.mark.parametrize("table", ["market_ticks", "macro_observations", "events", "swap_snapshots"])
def test_init_refuses_each_nonempty_table(pair, table):
    from psycopg import sql

    from trading.storage import research_mirror as mirror

    _, target = pair
    with mirror._connect(target) as conn:
        tick(conn)
        small_rows(conn)
        for other in mirror.TABLES:
            if other != table:
                conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier("public", other)))
    with pytest.raises(mirror.MirrorError, match="データ"):
        mirror.initialize(target)
    with mirror._connect(target) as conn, pytest.raises(mirror.MirrorError, match="印"):
        mirror._require_marker(conn)


@pytest.mark.parametrize("table", ["market_ticks", "macro_observations", "events", "swap_snapshots"])
def test_init_requires_each_mirror_table(pair, table):
    from psycopg import sql

    from trading.storage import research_mirror as mirror

    _, target = pair
    with mirror._connect(target) as conn:
        conn.execute(sql.SQL("ALTER TABLE {} RENAME TO test_missing_table").format(
            sql.Identifier("public", table)))
        try:
            with pytest.raises(mirror.MirrorError, match="対象4表"):
                mirror.initialize(target)
            with pytest.raises(mirror.MirrorError, match="印"):
                mirror._require_marker(conn)
        finally:
            conn.execute(sql.SQL("ALTER TABLE public.test_missing_table RENAME TO {}").format(
                sql.Identifier(table)))


def test_init_does_not_require_unrelated_tables_or_indexes(pair):
    from trading.storage import research_mirror as mirror

    _, target = pair
    with mirror._connect(target) as conn:
        definition = conn.execute(
            "SELECT pg_get_indexdef('public.idx_ticks_received_at'::regclass)"
        ).fetchone()[0]
        conn.execute("DROP INDEX public.idx_ticks_received_at")
        conn.execute("ALTER TABLE public.execution_state_observations RENAME TO test_unrelated_table")
        try:
            mirror.initialize(target)
            mirror._require_marker(conn)
        finally:
            conn.execute("ALTER TABLE public.test_unrelated_table RENAME TO execution_state_observations")
            conn.execute(definition)


@pytest.mark.parametrize("table", ["market_ticks", "macro_observations", "events", "swap_snapshots"])
def test_column_mismatch_refuses_sync_before_any_write(pair, table):
    from psycopg import sql

    from trading.storage import research_mirror as mirror

    source, target = pair
    mirror.initialize(target)
    with mirror._connect(source) as conn:
        tick(conn)
        small_rows(conn, suffix="new")
    with mirror._connect(target) as conn:
        small_rows(conn, suffix="keep")
        conn.execute(sql.SQL("ALTER TABLE {} ADD COLUMN test_extra TEXT").format(
            sql.Identifier("public", table)))
        try:
            before = {name: rows(target, name) for name in mirror.TABLES}
            with pytest.raises(mirror.MirrorError, match="列構成"):
                mirror.synchronize(source, target)
            assert before == {name: rows(target, name) for name in mirror.TABLES}
        finally:
            conn.execute(sql.SQL("ALTER TABLE {} DROP COLUMN test_extra").format(
                sql.Identifier("public", table)))


def test_same_database_is_rejected_without_dsn_string_comparison(pair):
    from psycopg.conninfo import make_conninfo

    from trading.storage import research_mirror as mirror

    _, target = pair
    mirror.initialize(target)
    alternate = make_conninfo(target, application_name="other-connection")
    with pytest.raises(mirror.MirrorError, match="同じ DB"):
        mirror.synchronize(alternate, target)
    assert rows(target, "market_ticks") == []


def test_concurrent_mirror_refuses_to_write(pair):
    from trading.storage import research_mirror as mirror

    source, target = pair
    mirror.initialize(target)
    with (mirror._connect(target) as holder, mirror._exclusive_target(holder),
          pytest.raises(mirror.MirrorError, match="実行中")):
        mirror.synchronize(source, target)


def test_rows_committed_after_ceiling_are_not_copied(pair, monkeypatch):
    from trading.storage import research_mirror as mirror

    source, target = pair
    mirror.initialize(target)
    with mirror._connect(source) as conn:
        first = tick(conn)
    original_pin = mirror._pin_ticks

    class WriteAfterCeiling:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, query, params=None):
            result = self.conn.execute(query, params)
            if isinstance(query, str) and "SELECT max(id)" in query:
                # max(id) の結果は既に確定。ロックの取得前に次の writer を走らせる。
                with mirror._connect(source) as writer:
                    tick(writer, second=10)
            return result

    def pin_then_write(conn, symbols):
        return original_pin(WriteAfterCeiling(conn), symbols)

    monkeypatch.setattr(mirror, "_pin_ticks", pin_then_write)
    result = mirror.synchronize(source, target)
    assert result.ceiling == first
    assert [row[0] for row in rows(target, "market_ticks")] == [first]


def test_pin_waits_for_lower_uncommitted_id_and_releases_writer_lock(pair, monkeypatch):
    from trading.storage import research_mirror as mirror

    source, target = pair
    mirror.initialize(target)
    reached_lock = threading.Event()
    completed = threading.Event()
    errors = []
    original_connect = mirror._connect

    class ObservedConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def execute(self, query, params=None):
            if isinstance(query, str) and "pg_advisory_xact_lock" in query:
                reached_lock.set()
            return self.conn.execute(query, params)

    def connect(dsn, **kwargs):
        conn = original_connect(dsn, **kwargs)
        return ObservedConnection(conn) if kwargs.get("read_only") else conn

    monkeypatch.setattr(mirror, "_connect", connect)

    def run():
        try:
            mirror.synchronize(source, target, mirror.MirrorOptions(chunk_size=1, sleep_seconds=0))
        except (mirror.psycopg.Error, mirror.MirrorError) as exc:
            errors.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(target=run)
    try:
        with original_connect(source) as writer, writer.transaction():
            writer.execute("SELECT pg_advisory_xact_lock(%s, hashtext('USDJPY'))",
                           (mirror._TICK_ADVISORY_LOCK_CLASS_ID,))
            lower = tick(writer)
            with original_connect(source) as other:
                tick(other, "EURUSD")
            worker.start()
            assert reached_lock.wait(5)
            assert not completed.wait(0.1)
    finally:
        if worker.ident is not None:
            worker.join(10)
    assert not worker.is_alive()
    assert errors == []
    assert [row[0] for row in rows(target, "market_ticks")] == [lower]


def test_ceiling_read_precedes_writer_lock(pair, monkeypatch):
    from trading.storage import research_mirror as mirror

    source, _ = pair
    pids, errors = [], []
    ready = threading.Event()
    monkeypatch.setattr(mirror, "_STREAM_SETTLE_TIMEOUT_SECONDS", 0.2)

    def pin():
        try:
            with mirror._connect(source, read_only=True) as reader:
                pids.append(reader.info.backend_pid)
                ready.set()
                mirror._pin_ticks(reader, ("USDJPY",))
        except (mirror.psycopg.Error, mirror.MirrorError) as exc:
            errors.append(exc)

    worker = threading.Thread(target=pin)
    with mirror._connect(source) as blocker, mirror._connect(source) as observer:
        observer.execute("SELECT pg_advisory_lock(%s, hashtext('USDJPY'))",
                         (mirror._TICK_ADVISORY_LOCK_CLASS_ID,))
        try:
            with blocker.transaction():
                blocker.execute("LOCK TABLE public.market_ticks IN ACCESS EXCLUSIVE MODE")
                worker.start()
                assert ready.wait(5)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    waiting = observer.execute(
                        "SELECT locktype FROM pg_locks WHERE pid = %s AND NOT granted", (pids[0],),
                    ).fetchall()
                    if waiting:
                        break
                    time.sleep(0.01)
                assert ("relation",) in waiting
                assert ("advisory",) not in waiting
            worker.join(5)
            assert not worker.is_alive()
        finally:
            observer.execute("SELECT pg_advisory_unlock(%s, hashtext('USDJPY'))",
                             (mirror._TICK_ADVISORY_LOCK_CLASS_ID,))
            worker.join(5)
    assert len(errors) == 1
    assert isinstance(errors[0], mirror.psycopg.errors.LockNotAvailable)


@pytest.mark.parametrize("table", ["market_ticks", "events"])
def test_count_mismatch_rolls_back_chunk_or_all_small_tables(pair, table):
    from psycopg import sql

    from trading.storage import research_mirror as mirror

    source, target = pair
    mirror.initialize(target)
    with mirror._connect(source) as conn:
        for second in range(3):
            tick(conn, second=second)
        small_rows(conn, suffix="new")
    with mirror._connect(target) as conn:
        small_rows(conn, suffix="old")
        before = {name: rows(target, name) for name in mirror.TABLES[1:]}
        body = ("IF NEW.id < 3 THEN RETURN NEW; END IF; RETURN NULL;"
                if table == "market_ticks" else "RETURN NULL;")
        conn.execute(sql.SQL("CREATE OR REPLACE FUNCTION public.test_discard() RETURNS trigger "
                             "LANGUAGE plpgsql AS {}").format(sql.Literal(f"BEGIN {body} END;")))
        conn.execute(sql.SQL("CREATE TRIGGER test_discard BEFORE INSERT ON {} "
                             "FOR EACH ROW EXECUTE FUNCTION public.test_discard()").format(
                                 sql.Identifier("public", table)))
        try:
            report = mirror.MirrorReport()
            with pytest.raises(mirror.MirrorError, match="行数"):
                mirror.synchronize(source, target, mirror.MirrorOptions(chunk_size=2, sleep_seconds=0),
                                   report=report)
            if table == "market_ticks":
                assert [row[0] for row in rows(target, "market_ticks")] == [1, 2]
                assert report.rows["market_ticks"] == 2
            assert before == {name: rows(target, name) for name in mirror.TABLES[1:]}
        finally:
            conn.execute(sql.SQL("DROP TRIGGER test_discard ON {}").format(
                sql.Identifier("public", table)))
            conn.execute("DROP FUNCTION public.test_discard()")
    resumed = mirror.synchronize(source, target, mirror.MirrorOptions(sleep_seconds=0))
    assert resumed.rows["market_ticks"] == (1 if table == "market_ticks" else 0)
    for name in mirror.TABLES:
        assert rows(source, name) == rows(target, name)


def test_interrupt_after_committed_chunk_can_resume(pair, monkeypatch):
    from trading.storage import research_mirror as mirror

    source, target = pair
    mirror.initialize(target)
    with mirror._connect(source) as conn:
        for second in range(5):
            tick(conn, second=second)

    def interrupt(_):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(mirror.time, "sleep", interrupt)
        with pytest.raises(KeyboardInterrupt):
            mirror.synchronize(source, target, mirror.MirrorOptions(chunk_size=2))
    assert len(rows(target, "market_ticks")) == 2
    result = mirror.synchronize(source, target, mirror.MirrorOptions(chunk_size=2, sleep_seconds=0))
    assert result.rows["market_ticks"] == 3
    assert rows(source, "market_ticks") == rows(target, "market_ticks")
