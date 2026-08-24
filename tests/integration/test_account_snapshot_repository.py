"""PostgreSQL-backed account snapshot series.

Requires TRADING_DB_DSN (see tests/integration/README.md); skipped without it.

Nothing wrote this table before the account collector existed, so the insert
path had never run against a real database. The rows use observed_at far in
the future to stay clear of collected data — the table has no key to scope a
test by, and `latest()` reads the whole series — and each test removes them.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.support import make_snapshot

DSN = os.environ.get("TRADING_DB_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")

FUTURE = datetime(2099, 3, 1, tzinfo=UTC)


@pytest.fixture
def repo():
    from trading.storage.postgres import PostgresAccountSnapshotRepository, connect

    conn = connect(DSN)
    yield PostgresAccountSnapshotRepository(conn)
    conn.execute("DELETE FROM account_snapshots WHERE observed_at >= %s", (FUTURE,))
    conn.commit()
    conn.close()


def at(**kwargs) -> datetime:
    return FUTURE + timedelta(**kwargs)


def test_a_snapshot_round_trips_through_the_database(repo):
    written = make_snapshot(
        "1002000",
        observed_at=FUTURE,
        high_water_mark="1010000",
        margin="50000",
        margin_level="2030.1",
        balance="1000500",
    )
    repo.insert(written)

    (read,) = repo.since(FUTURE)

    assert read == written


def test_an_absent_margin_level_round_trips_as_none(repo):
    # A flat book has no ratio to report, and the column is nullable so that
    # stays distinguishable from a level of zero.
    repo.insert(make_snapshot("1000000", observed_at=FUTURE))

    (read,) = repo.since(FUTURE)

    assert read.margin_level is None


def test_since_returns_the_window_oldest_first(repo):
    for hour in (2, 0, 1):
        repo.insert(make_snapshot("1000000", observed_at=at(hours=hour)))

    window = repo.since(at(hours=1))

    assert [s.observed_at for s in window] == [at(hours=1), at(hours=2)]


def test_latest_returns_the_newest_observation(repo):
    repo.insert(make_snapshot("1000000", observed_at=FUTURE))
    repo.insert(make_snapshot("1005000", observed_at=at(hours=3)))

    latest = repo.latest()

    assert latest is not None
    assert latest.observed_at == at(hours=3)
    assert latest.equity == Decimal(1005000)
