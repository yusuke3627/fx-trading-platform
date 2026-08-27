"""PostgreSQL-backed swap_snapshots の PIT 読み出し。

Requires TRADING_DB_DSN (see tests/integration/README.md); skipped without it.
Each test uses a unique throwaway symbol and removes its own rows.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading.domain.swap import SwapSnapshot

DSN = os.environ.get("TRADING_DB_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")

T0 = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)


@pytest.fixture
def repo():
    from trading.storage.postgres import PostgresSwapSnapshotRepository, connect

    conn = connect(DSN)
    symbol = f"TESTFX{uuid4().hex[:8]}"
    yield PostgresSwapSnapshotRepository(conn), symbol
    conn.execute("DELETE FROM swap_snapshots WHERE symbol = %s", (symbol,))
    conn.commit()
    conn.close()


def snapshot(symbol: str, known_offset_hours: int = 0, **overrides) -> SwapSnapshot:
    known_at = T0 + timedelta(hours=known_offset_hours)
    values = {
        "snapshot_id": uuid4(),
        "symbol": symbol,
        "swap_mode": 1,
        "swap_long": Decimal("-2.2"),
        "swap_short": Decimal("0.4"),
        "swap_rollover3days": 3,
        "swap_wednesday": 3,
        "retrieved_at": known_at,
        "known_at": known_at,
    }
    values.update(overrides)
    return SwapSnapshot(**values)


def test_roundtrip_and_latest_known_before(repo):
    repository, symbol = repo
    early = snapshot(symbol)
    late = snapshot(symbol, known_offset_hours=24, swap_long=Decimal("-9.9"))
    repository.insert(early)
    repository.insert(late)

    visible = repository.known_before(symbol, T0 + timedelta(hours=48))
    assert [s.known_at for s in visible] == [early.known_at, late.known_at]
    stored = visible[0]
    assert stored.swap_long == Decimal("-2.2")
    assert stored.swap_wednesday == 3
    assert stored.swap_monday is None

    at_boundary = repository.latest_known_before(symbol, T0 + timedelta(hours=12))
    assert at_boundary is not None
    assert at_boundary.snapshot_id == early.snapshot_id
    assert repository.latest_known_before(symbol, T0 - timedelta(hours=1)) is None


def test_same_known_at_order_is_fixed_by_id(repo):
    repository, symbol = repo
    first = snapshot(symbol)
    second = snapshot(symbol, swap_long=Decimal("-9.9"))
    repository.insert(first)
    repository.insert(second)

    rows = repository.known_before(symbol, T0 + timedelta(hours=1))
    assert [s.snapshot_id for s in rows] == sorted(
        [first.snapshot_id, second.snapshot_id]
    )
    latest = repository.latest_known_before(symbol, T0 + timedelta(hours=1))
    assert latest is not None
    assert latest.snapshot_id == max(first.snapshot_id, second.snapshot_id)
