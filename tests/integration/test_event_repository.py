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
