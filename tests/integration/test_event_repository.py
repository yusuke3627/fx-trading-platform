"""PostgreSQL-backed visibility of the events table.

Requires TRADING_DB_DSN (see tests/integration/README.md); skipped without it.
Each test uses a unique throwaway event_type and removes its own rows.

known_before builds its WHERE clause from which filters are present, and only
a real database can show that every combination selects what it claims.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading.domain.event import EventEnvelope

DSN = os.environ.get("TRADING_DB_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")

T0 = datetime(2026, 8, 12, 12, 30, tzinfo=UTC)


@pytest.fixture
def repo():
    from trading.storage.postgres import PostgresEventRepository, connect

    conn = connect(DSN)
    event_type = f"TEST_EVENT_{uuid4().hex[:12].upper()}"
    yield PostgresEventRepository(conn), event_type
    conn.execute("DELETE FROM events WHERE event_type LIKE %s", (f"{event_type}%",))
    conn.commit()
    conn.close()


def event(event_type: str, known_offset_hours: int = 0) -> EventEnvelope:
    at = T0 + timedelta(hours=known_offset_hours)
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        source="TEST",
        payload={"offset": known_offset_hours},
        retrieved_at=at,
        known_at=at,
    )


def test_upsert_inserts_corrects_and_leaves_identical_facts_alone(repo):
    # Deterministic-id ingests (policy meeting scores) correct transcription
    # errors by editing the curated file; the store must follow the file, and
    # a run that changes nothing must not churn the stored rows.
    r, event_type = repo
    first = event(event_type, 0)
    assert r.upsert(first) == "inserted"

    # retrieved_at moves on every run but is not a fact: no correction.
    rerun = first.model_copy(update={"retrieved_at": T0 + timedelta(days=1)})
    assert r.upsert(rerun) == "unchanged"
    stored = r.known_before(T0 + timedelta(days=30), event_type)
    assert stored[0].retrieved_at == T0

    corrected = first.model_copy(
        update={
            "payload": {"offset": 99},
            "known_at": T0 + timedelta(hours=1),
            "retrieved_at": T0 + timedelta(days=2),
        }
    )
    assert r.upsert(corrected) == "updated"
    stored = r.known_before(T0 + timedelta(days=30), event_type)
    assert [e.payload["offset"] for e in stored] == [99]
    assert stored[0].known_at == T0 + timedelta(hours=1)
    assert stored[0].retrieved_at == T0 + timedelta(days=2)


def test_every_filter_combination_selects_what_it_claims(repo):
    r, event_type = repo
    other_type = f"{event_type}_OTHER"
    r.insert(event(event_type, 0))
    r.insert(event(event_type, 24))
    r.insert(event(other_type, 24))
    horizon = T0 + timedelta(days=30)

    by_time = r.known_before(T0 + timedelta(hours=1))
    assert [e.payload["offset"] for e in by_time if e.event_type.startswith(event_type)] == [0]

    by_type = r.known_before(horizon, event_type)
    assert [e.payload["offset"] for e in by_type] == [0, 24]

    # since is exclusive: a row known exactly at the bound is outside it.
    windowed = r.known_before(horizon, event_type, since=T0)
    assert [e.payload["offset"] for e in windowed] == [24]

    both_types_windowed = r.known_before(horizon, since=T0)
    offsets = [
        e.payload["offset"] for e in both_types_windowed if e.event_type.startswith(event_type)
    ]
    assert offsets == [24, 24]


@pytest.mark.parametrize("same_time", [False, True])
def test_raw_archive_preserves_changes_but_skips_identical_retrievals(repo, same_time):
    from trading.data.macro.base import raw_event

    r, event_type = repo
    saved = []
    for index, value in enumerate(("A", "A", "B", "B", "A", "A")):
        at = T0 if same_time else T0 + timedelta(hours=index)
        raw = raw_event(
            source="TEST", source_uri="https://example.invalid/raw",
            payload={"value": value}, retrieved_at=at,
        ).model_copy(update={"event_type": event_type})
        inserted = r.insert_raw_archive(raw)
        assert inserted is (index in (0, 2, 4))
        if inserted:
            saved.append(raw)
    stored = r.known_before(T0 + timedelta(days=1), event_type)
    assert {e.event_id: e for e in stored} == {e.event_id: e for e in saved}


def test_raw_archive_scopes_comparison_by_uri_and_type(repo):
    from trading.data.macro.base import payload_hash

    r, event_type = repo
    first = event(event_type).model_copy(update={
        "source_uri": "https://example.invalid/one", "payload_hash": payload_hash({"offset": 0}),
    })
    assert r.insert_raw_archive(first)
    for updates in ({"source_uri": "https://example.invalid/two"},
                    {"event_type": f"{event_type}_OTHER"}):
        assert r.insert_raw_archive(first.model_copy(update={"event_id": uuid4(), **updates}))
    # 読み取りが開いた transaction の後でも、保存結果は別 connection に見える。
    r.known_before(T0, event_type)
    assert not r.insert_raw_archive(first.model_copy(update={"event_id": uuid4()}))
    from trading.storage.postgres import connect
    with connect(DSN) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM events WHERE event_type LIKE %s",
            (f"{event_type}%",),
        ).fetchone()["n"] == 3


def test_concurrent_raw_archives_store_only_one_snapshot(repo):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from trading.data.macro.base import raw_event
    from trading.storage.postgres import PostgresEventRepository, connect

    _, event_type = repo
    barrier = Barrier(4)

    def archive(_):
        raw = raw_event(
            source="TEST", source_uri="https://example.invalid/concurrent",
            payload={"value": "unchanged"}, retrieved_at=T0,
        ).model_copy(update={"event_type": event_type})
        with connect(DSN) as conn:
            barrier.wait(timeout=10)
            return PostgresEventRepository(conn).insert_raw_archive(raw)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(archive, range(4))) == [False, False, False, True]


@pytest.mark.parametrize("field", ["source_uri", "payload_hash"])
def test_raw_archive_requires_deduplication_metadata(repo, field):
    r, event_type = repo
    raw = event(event_type).model_copy(update={
        "source_uri": "https://example.invalid/raw", "payload_hash": "digest", field: None,
    })
    with pytest.raises(ValueError, match="source_uri and payload_hash"):
        r.insert_raw_archive(raw)


def test_macro_store_skips_raw_duplicates_without_skipping_observations(repo):
    from unittest.mock import Mock

    from trading.data.macro.base import CollectionBatch, raw_event
    from trading.data.macro.collector import _store

    r, event_type = repo
    observations = Mock()
    observations.insert_many.return_value = 0
    for _ in range(2):
        raw = raw_event(
            source="TEST", source_uri="https://example.invalid/macro",
            payload={"observations": []}, retrieved_at=T0,
        ).model_copy(update={"event_type": event_type})
        assert _store(CollectionBatch(observations=(), raw_events=(raw,)), observations, r) == 0
    assert observations.insert_many.call_count == 2
    assert len(r.known_before(T0, event_type)) == 1


def test_intervention_cli_archives_raw_once_and_preserves_parsed_events(repo, monkeypatch, capsys):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from trading.data.intervention import collector
    from trading.data.macro.base import raw_event

    r, event_type = repo
    parsed = [event(f"{event_type}_PARSED"), event(f"{event_type}_PARSED")]
    monkeypatch.setenv("TEST_RAW_ARCHIVE_DSN", DSN)
    monkeypatch.setattr("sys.argv", ["collector"])
    monkeypatch.setattr("trading.config.load_config", lambda _: SimpleNamespace(
        storage=SimpleNamespace(dsn_env="TEST_RAW_ARCHIVE_DSN"),
    ))
    monkeypatch.setattr(collector, "load_episodes", lambda _: [])
    for index, collector_name in enumerate(("MOFDailyCollector", "MOFMonthlyCollector")):
        def batch(index=index, **_):
            raw = raw_event(
                source="TEST", source_uri=f"https://example.invalid/intervention/{index}",
                payload={"content": str(index)}, retrieved_at=T0,
            ).model_copy(update={"event_type": event_type})
            return SimpleNamespace(raw_events=(raw,), events=(parsed[index],))
        factory = Mock()
        factory.return_value.collect.side_effect = batch
        monkeypatch.setattr(collector, collector_name, factory)
    collector.main()
    assert "parsed 2 events, stored 2 new" in capsys.readouterr().out
    collector.main()
    assert "parsed 2 events, stored 0 new" in capsys.readouterr().out
    assert len(r.known_before(T0, event_type)) == 2
    assert len(r.known_before(T0, f"{event_type}_PARSED")) == 2


def test_raw_archive_same_time_uses_insert_time_after_an_earlier_read(repo):
    from trading.data.macro.base import raw_event
    from trading.storage.postgres import PostgresEventRepository, connect

    r, event_type = repo
    # r の transaction は他 connection による保存前から始まっている。
    r.known_before(T0, event_type)
    with connect(DSN) as conn:
        first = raw_event(
            source="TEST", source_uri="https://example.invalid/read-first",
            payload={"value": "A"}, retrieved_at=T0,
        ).model_copy(update={"event_type": event_type})
        assert PostgresEventRepository(conn).insert_raw_archive(first)
    second = raw_event(
        source="TEST", source_uri=first.source_uri,
        payload={"value": "B"}, retrieved_at=T0,
    ).model_copy(update={"event_type": event_type})
    assert r.insert_raw_archive(second)
    assert not r.insert_raw_archive(second.model_copy(update={"event_id": uuid4()}))
