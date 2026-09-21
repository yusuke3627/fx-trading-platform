"""状態保存と観測の原子性、およびread-only snapshot exportを実DBで確認する。"""
import json
import os
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.support import T0, at, make_command, usdjpy_spec
from trading.backtest.execution_quality_export import build_input, main
from trading.backtest.execution_quality_study import main as study_main
from trading.domain.order import CommandState
from trading.oms.claim import mark_broker_request_started
from trading.oms.state_machine import transition
from trading.storage.execution_observations import PostgresExecutionObservationReader
from trading.storage.repository import StaleCommandStateError

DSN = os.environ.get("TRADING_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")
END = at(seconds=20)


@pytest.fixture
def db():
    from trading.storage.postgres import PostgresCommandRepository, connect

    with connect(DSN) as conn:
        conn.execute("DELETE FROM fills")
        conn.execute("DELETE FROM execution_commands")
        conn.commit()
        yield conn, PostgresCommandRepository(conn)
        conn.rollback()
        conn.execute("DELETE FROM fills")
        conn.execute("DELETE FROM execution_commands")
        conn.commit()


def insert_command(repo, state=CommandState.CREATED):
    command = make_command(state=state).model_copy(update={"intent_id": None})
    repo.insert(command)
    return command


def advance(repo, command, state, seconds):
    changed = transition(command, state, now=at(seconds=seconds))
    repo.save_state(changed, command.state)
    return changed


def observations(conn):
    return conn.execute("SELECT * FROM execution_state_observations ORDER BY id").fetchall()


def read(conn, *, end=END, max_rows=1000):
    conn.commit()
    return PostgresExecutionObservationReader(conn).read(
        T0, end, at(seconds=-2), max_rows=max_rows,
    )


def convert(rows, *, end=END):
    return build_input(
        rows, start=T0, end=end, basis="simulated", instruments=(usdjpy_spec(),),
        horizons=(Decimal(1),), quote_max_age=Decimal(2), provenance="架空のDB観測",
    )[0]


def test_transitions_and_claim_are_recorded_but_request_marker_is_not_a_transition(db):
    conn, repo = db
    command = insert_command(repo)
    command = advance(repo, command, CommandState.RISK_APPROVED, 1)
    command = advance(repo, command, CommandState.READY, 2)
    command = repo.claim_next("example-worker", 30, at(seconds=3))
    command = advance(repo, command, CommandState.SUBMITTING, 4)
    started = mark_broker_request_started(command, at(seconds=5))
    repo.save_state(started, command.state)

    records = observations(conn)
    assert [r["state_revision"] for r in records] == list(range(5))
    assert [r["changed_at"] for r in records] == [at(seconds=i) for i in range(5)]
    data = convert(read(conn))
    assert data.orders[0].history_complete
    assert data.orders[0].sent_at is None
    assert data.orders[0].states[-1].at.at == at(seconds=4)


def test_skipped_transitions_leave_revision_gaps_and_incomplete_history(db):
    conn, repo = db
    command = insert_command(repo)
    approved = transition(command, CommandState.RISK_APPROVED, now=at(seconds=1))
    ready = transition(approved, CommandState.READY, now=at(seconds=2))
    repo.save_state(ready, command.state)
    assert [r["state_revision"] for r in observations(conn)] == [0, 2]
    # 現在の snapshot まで窓を広げれば、不完全な履歴も欠測付きで出せる。
    end = conn.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
    assert not convert(read(conn, end=end), end=end).orders[0].history_complete


def test_failed_cas_does_not_append_observations(db):
    conn, repo = db
    command = insert_command(repo)
    advance(repo, command, CommandState.REJECTED, 1)
    stale = transition(command, CommandState.EXPIRED, now=at(seconds=2))
    with pytest.raises(StaleCommandStateError):
        repo.save_state(stale, CommandState.CREATED)
    assert [r["state"] for r in observations(conn)] == ["CREATED", "REJECTED"]


def test_journal_failure_rolls_back_command_even_on_autocommit_connection(db):
    import psycopg

    conn, repo = db
    conn.autocommit = True
    conn.execute("ALTER TABLE execution_state_observations ADD CONSTRAINT example_failure CHECK (quantity <> 731)")
    command = make_command(quantity="731").model_copy(update={"intent_id": None})
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            repo.insert(command)
        assert repo.get(str(command.command_id)) is None
        assert observations(conn) == []
    finally:
        conn.execute("ALTER TABLE execution_state_observations DROP CONSTRAINT example_failure")


@pytest.mark.parametrize("autocommit", [False, True])
def test_conflicting_partial_fill_revision_rolls_back_but_identical_resave_is_allowed(db, autocommit):
    from trading.storage.postgres import PostgresCommandRepository, connect

    conn, repo = db
    command = insert_command(repo)
    for seconds, state in enumerate((CommandState.RISK_APPROVED, CommandState.READY,
                                    CommandState.CLAIMED, CommandState.SUBMITTING,
                                    CommandState.PARTIAL_FILL), 1):
        command = advance(repo, command, state, seconds)
    with connect(DSN) as other:
        other.autocommit = autocommit
        stale_repo = PostgresCommandRepository(other)
        stale = stale_repo.get(str(command.command_id))
        saved = advance(repo, command, CommandState.PARTIAL_FILL, 6)
        conflicting = transition(stale, CommandState.PARTIAL_FILL, now=at(seconds=7))
        with pytest.raises(StaleCommandStateError, match="observation revision conflicts"):
            stale_repo.save_state(conflicting, CommandState.PARTIAL_FILL)
        stored = stale_repo.get(str(command.command_id))
        assert stored.state_revision == saved.state_revision
        assert stored.state_changed_at == at(seconds=6)
        other.rollback()

    before = observations(conn)
    repo.save_state(saved, CommandState.PARTIAL_FILL)
    assert observations(conn) == before
    # 古い非遷移オブジェクトの再保存でrevisionを巻き戻してもならない。
    with pytest.raises(StaleCommandStateError):
        repo.save_state(stale, CommandState.PARTIAL_FILL)
    assert repo.get(str(command.command_id)).state_changed_at == at(seconds=6)
    assert convert(read(conn)).orders[0].history_complete


def test_persisted_late_fill_does_not_enter_an_older_read_snapshot(db):
    from trading.storage.postgres import connect

    conn, repo = db
    command = insert_command(repo)
    settings = []

    class InterleavedConnection:
        transaction = conn.transaction

        def execute(self, query, params=None):
            result = conn.execute(query, params)
            if "SELECT c.*, s.generated_at" in query:
                settings.append(conn.execute(
                    "SELECT current_setting('transaction_read_only') AS readonly, "
                    "current_setting('transaction_isolation') AS isolation"
                ).fetchone())
                with connect(DSN) as writer:
                    writer.execute(
                        """INSERT INTO fills (id, broker_deal_id, execution_command_id, origin,
                           side, quantity, price, broker_time, received_at)
                           VALUES (%s, %s, %s, 'COMMAND', 'SELL', 1000, 150, %s, %s)""",
                        (uuid4(), uuid4().hex, command.command_id, at(hours=3), at(seconds=1)),
                    )
            return result

    rows = PostgresExecutionObservationReader(InterleavedConnection()).read(
        T0, at(seconds=20), T0, max_rows=100,
    )
    assert rows["counts"]["commands"] == 1
    assert rows["fills"] == []
    assert list(settings[0].values()) == ["on", "repeatable read"]
    assert conn.execute("SELECT count(*) AS n FROM fills").fetchone()["n"] == 1


def test_export_limit_never_silently_truncates_orders(db):
    conn, repo = db
    insert_command(repo)
    insert_command(repo)
    with pytest.raises(ValueError, match="commands が max_rows"):
        read(conn, max_rows=1)


def test_quote_export_uses_receipt_clock_and_requires_explicit_provenance(db):
    conn, repo = db
    insert_command(repo)
    run_id = uuid4()
    try:
        for index, received in enumerate((at(seconds=-1), at(seconds=1), at(seconds=21))):
            conn.execute(
                """INSERT INTO market_ticks (symbol, bid, ask, event_time, received_at, source, ingestion_run)
                   VALUES ('USDJPY', %s, %s, %s, %s, 'EXAMPLE', %s)""",
                (Decimal(160 + index), Decimal(160 + index) + Decimal(".01"),
                 at(hours=3, seconds=index), received, run_id),
            )
        conn.commit()
        assert read(conn)["quotes"] == []
        rows = PostgresExecutionObservationReader(conn).read(
            T0, END, at(seconds=-2), max_rows=100, include_quotes=True,
        )
        assert [row["received_at"] for row in rows["quotes"]] == [at(seconds=-1), at(seconds=1)]
        assert [row["bid"] for row in rows["quotes"]] == [Decimal(160), Decimal(161)]
    finally:
        conn.execute("DELETE FROM market_ticks WHERE ingestion_run = %s", (run_id,))
        conn.commit()


def test_export_and_study_cli_keep_rejected_expired_and_unknown_orders(db, tmp_path, monkeypatch):
    conn, repo = db
    for path in (
        (CommandState.REJECTED,), (CommandState.EXPIRED,),
        (CommandState.RISK_APPROVED, CommandState.READY, CommandState.CLAIMED, CommandState.UNKNOWN),
    ):
        command = insert_command(repo)
        for seconds, state in enumerate(path, 1):
            command = advance(repo, command, state, seconds)
    end = conn.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
    conn.commit()
    specs = tmp_path / "instruments.json"
    specs.write_text(json.dumps([usdjpy_spec().model_dump(mode="json")]))
    monkeypatch.setenv("EXPLICIT_OBSERVATION_TEST_DSN", DSN)
    output = tmp_path / "export"
    assert main([
        "--dsn-env", "EXPLICIT_OBSERVATION_TEST_DSN", "--start", T0.isoformat(),
        "--end", end.isoformat(), "--time-basis", "simulated",
        "--provenance", "専用テストDBの架空注文", "--instruments", str(specs),
        "--horizon-seconds", "1", "--quote-max-age-seconds", "2", "--output-dir", str(output),
    ]) == 0
    report_dir = tmp_path / "study"
    assert study_main(["--input", str(output / "input.json"), "--output-dir", str(report_dir)]) == 0
    report = json.loads((report_dir / "report.json").read_text())
    assert report["orders_total"] == 3
    assert report["symbols"][0]["final_states"] == {"REJECTED": 1, "EXPIRED": 1, "UNKNOWN": 1}
    assert report["symbols"][0]["pending_statuses"] == {"incomplete_fill_history": 3}
    assert "3" in (report_dir / "report.md").read_text()
