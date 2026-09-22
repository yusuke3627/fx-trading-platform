"""raw archive のハッシュ参照契約。DB 接続は使わない。"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from tests.support import FakeEventRepository
from trading.domain.event import EventEnvelope


@pytest.fixture
def raw_event():
    return EventEnvelope(
        event_id=uuid4(), event_type="FICTIONAL_RAW", source="TEST",
        source_uri="https://example.invalid/raw", payload_hash="original",
        known_at=datetime(2026, 8, 1, tzinfo=UTC),
        retrieved_at=datetime(2026, 9, 19, tzinfo=UTC),
    )


def test_latest_hash_uses_known_time_then_insertion_order_and_filters_scope(raw_event):
    repository = FakeEventRepository()
    event_type, uri = raw_event.event_type, raw_event.source_uri
    assert repository.latest_raw_hash(event_type, uri) is None
    newest = raw_event.model_copy(update={
        "event_id": uuid4(), "payload_hash": "latest",
        "known_at": raw_event.known_at + timedelta(days=1),
    })
    assert repository.insert_raw_archive(newest)
    assert repository.insert_raw_archive(raw_event)
    assert repository.latest_raw_hash(event_type, uri) == "latest"
    same_time = newest.model_copy(update={"event_id": uuid4(), "payload_hash": "tie-winner"})
    assert repository.insert_raw_archive(same_time)
    assert repository.latest_raw_hash(event_type, uri) == "tie-winner"
    for overrides in [
        {"source_uri": "https://example.invalid/other"},
        {"event_type": "FICTIONAL_OTHER_RAW"},
        {"payload_hash": None},
    ]:
        repository.events.append(newest.model_copy(update={
            "event_id": uuid4(), "known_at": newest.known_at + timedelta(days=1), **overrides,
        }))
    assert repository.latest_raw_hash(event_type, uri) == "tie-winner"


@pytest.mark.parametrize("digest", [None, "latest-hash"])
def test_postgres_lookup_selects_only_one_hash_and_never_loads_payload(digest):
    pytest.importorskip("psycopg")
    from trading.storage.postgres import PostgresEventRepository

    connection = MagicMock()
    cursor = connection.execute.return_value
    cursor.fetchone.return_value = None if digest is None else {"payload_hash": digest}
    repository = PostgresEventRepository(connection)
    assert repository.latest_raw_hash("FICTIONAL_RAW", "https://example.invalid/raw") == digest
    query, parameters = connection.execute.call_args.args
    sql = " ".join(query.split())
    assert sql.startswith("SELECT payload_hash FROM events WHERE")
    assert "payload_hash IS NOT NULL" in sql
    assert sql.endswith("ORDER BY known_at DESC, created_at DESC, id DESC LIMIT 1")
    assert parameters == ("https://example.invalid/raw", "FICTIONAL_RAW")
    cursor.fetchone.assert_called_once_with()
    cursor.fetchall.assert_not_called()


@pytest.mark.parametrize("existing_hash", [None, "original", "other-version"])
def test_postgres_initial_guard_checks_latest_hash_under_the_insert_lock(raw_event, existing_hash):
    pytest.importorskip("psycopg")
    from trading.storage.postgres import PostgresEventRepository

    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (
        None if existing_hash is None else {"payload_hash": existing_hash}
    )
    connection.execute.return_value.rowcount = 0 if existing_hash == "original" else 1
    repository = PostgresEventRepository(connection)
    if existing_hash == "other-version":
        with pytest.raises(ValueError, match="初回判定後"):
            repository.insert_raw_archive(raw_event, require_initial=True)
        connection.commit.assert_not_called()
        assert connection.transaction.return_value.__exit__.call_args.args[0] is ValueError
    else:
        assert repository.insert_raw_archive(raw_event, require_initial=True) == (
            existing_hash is None
        )
        connection.commit.assert_called_once_with()
    queries = [" ".join(call.args[0].split()) for call in connection.execute.call_args_list]
    assert "pg_advisory_xact_lock" in queries[0]
    assert queries[1].startswith("SELECT payload_hash FROM events")
    assert any(query.startswith("INSERT INTO events") for query in queries) == (
        existing_hash != "other-version"
    )


@pytest.mark.parametrize("method", ["insert", "insert_new", "insert_raw_archive", "upsert"])
def test_event_writes_reject_mutated_payload_before_database_access(raw_event, method):
    pytest.importorskip("psycopg")
    from trading.storage.postgres import PostgresEventRepository

    raw_event.payload["nested"] = {"amount": Decimal("1.25")}
    connection = MagicMock()
    repository = PostgresEventRepository(connection)

    with pytest.raises(ValueError):
        getattr(repository, method)(raw_event)

    assert connection.mock_calls == []
