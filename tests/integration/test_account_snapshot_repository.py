"""PostgreSQL-backed account snapshot series.

Requires TRADING_DB_DSN (see tests/integration/README.md); skipped without it.
Each test uses a throwaway account_id and removes its own rows.

Nothing wrote this table before the account collector existed, so the insert
path had never run against a real database.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.support import make_snapshot

DSN = os.environ.get("TRADING_DB_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")

T0 = datetime(2026, 8, 13, tzinfo=UTC)
# Well past every row a test writes, for reads that are not about visibility.
LATER = T0 + timedelta(days=1)


@pytest.fixture
def repo():
    from trading.storage.postgres import PostgresAccountSnapshotRepository, connect

    conn = connect(DSN)
    # Two accounts: the second is what the scoping is checked against.
    ours = f"test-{uuid4().hex[:12]}"
    other = f"test-{uuid4().hex[:12]}"
    yield PostgresAccountSnapshotRepository(conn), ours, other
    conn.execute(
        "DELETE FROM account_snapshots WHERE account_id = ANY(%s)", ([ours, other],)
    )
    conn.commit()
    conn.close()


def at(**kwargs) -> datetime:
    return T0 + timedelta(**kwargs)


def test_a_snapshot_round_trips_through_the_database(repo):
    r, account_id, _ = repo
    written = make_snapshot(
        "1002000",
        observed_at=T0,
        high_water_mark="1010000",
        margin="50000",
        margin_level="2030.1",
        balance="1000500",
    )
    r.insert(account_id, written)

    (read,) = r.known_before(account_id, LATER, T0)

    assert read == written


def test_an_absent_margin_level_round_trips_as_none(repo):
    # A flat book has no ratio to report, and the column is nullable so that
    # stays distinguishable from a level of zero.
    r, account_id, _ = repo
    r.insert(account_id, make_snapshot("1000000", observed_at=T0))

    (read,) = r.known_before(account_id, LATER, T0)

    assert read.margin_level is None


def test_since_returns_the_window_oldest_first(repo):
    r, account_id, _ = repo
    for hour in (2, 0, 1):
        r.insert(account_id, make_snapshot("1000000", observed_at=at(hours=hour)))

    window = r.known_before(account_id, LATER, at(hours=1))

    assert [s.observed_at for s in window] == [at(hours=1), at(hours=2)]


def test_latest_returns_the_newest_observation(repo):
    r, account_id, _ = repo
    r.insert(account_id, make_snapshot("1000000", observed_at=T0))
    r.insert(account_id, make_snapshot("1005000", observed_at=at(hours=3)))

    latest = r.latest_known_before(account_id, LATER)

    assert latest is not None
    assert latest.observed_at == at(hours=3)
    assert latest.equity == Decimal(1005000)


def test_a_row_observed_after_the_cutoff_is_not_visible(repo):
    # A live evaluation freezes its clock for the length of a cycle while the
    # account collector writes from its own process, so a row can land partway
    # through one. It was not knowable when the cycle began.
    r, account_id, _ = repo
    r.insert(account_id, make_snapshot("1000000", observed_at=T0))
    r.insert(account_id, make_snapshot("2000000", observed_at=at(hours=2)))

    latest = r.latest_known_before(account_id, at(hours=1))

    assert latest is not None and latest.equity == Decimal(1000000)
    assert [s.observed_at for s in r.known_before(account_id, at(hours=1), T0)] == [T0]


def test_another_accounts_rows_are_never_read(repo):
    # The scoping is what keeps a demo high-water mark out of a live account's
    # drawdown, so it is checked against the database and not only the fake.
    r, account_id, other = repo
    r.insert(other, make_snapshot("9000000", observed_at=at(hours=5)))
    r.insert(account_id, make_snapshot("1000000", observed_at=T0))

    latest = r.latest_known_before(account_id, LATER)

    assert latest is not None and latest.equity == Decimal(1000000)
    assert [s.equity for s in r.known_before(account_id, LATER, T0)] == [Decimal(1000000)]
