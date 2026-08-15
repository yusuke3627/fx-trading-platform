"""PostgreSQL-backed vintage semantics of macro_observations.

Requires TRADING_DB_DSN (see tests/integration/README.md); skipped without it.
Each test uses a unique throwaway series name and removes its own rows.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading.domain.economic import EconomicObservation

DSN = os.environ.get("TRADING_DB_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="TRADING_DB_DSN is not set")

T0 = datetime(2026, 8, 12, 12, 30, tzinfo=UTC)


@pytest.fixture
def repo():
    from trading.storage.postgres import PostgresMacroObservationRepository, connect

    conn = connect(DSN)
    series = f"test_series_{uuid4().hex[:12]}"
    yield PostgresMacroObservationRepository(conn), series
    conn.execute("DELETE FROM macro_observations WHERE series = %s", (series,))
    conn.commit()
    conn.close()


def observation(series: str, value: str, known_offset_hours: int = 0, **overrides):
    values = {
        "observation_id": uuid4(),
        "series": series,
        "observation_period": "2026-07",
        "value": Decimal(value),
        "unit": "index",
        "source": "TEST",
        "retrieved_at": T0 + timedelta(hours=known_offset_hours),
        "known_at": T0 + timedelta(hours=known_offset_hours),
    }
    values.update(overrides)
    return EconomicObservation(**values)


def test_unchanged_value_is_not_a_new_vintage(repo):
    r, series = repo
    assert r.insert_many([observation(series, "321.5")]) == 1
    # Scheduled re-collection: same value, later known_at -> no new row.
    assert r.insert_many([observation(series, "321.5", known_offset_hours=24)]) == 0
    assert len(r.known_before(series, T0 + timedelta(days=30))) == 1


def test_changed_value_appends_a_revision(repo):
    r, series = repo
    r.insert_many([observation(series, "321.5")])
    assert r.insert_many([observation(series, "321.7", known_offset_hours=24)]) == 1
    chain = r.known_before(series, T0 + timedelta(days=30))
    assert [o.value for o in chain] == [Decimal("321.5"), Decimal("321.7")]
    assert chain[0].known_at < chain[1].known_at


def test_exact_duplicate_vintage_is_ignored(repo):
    r, series = repo
    first = observation(series, "321.5")
    assert r.insert_many([first]) == 1
    # Same (series, period, known_at): unique key, not the value comparison.
    assert r.insert_many([observation(series, "999.9")]) == 0


def test_backfill_before_existing_forward_row_is_kept(repo):
    r, series = repo
    # Forward collection stored today's value first...
    assert r.insert_many([observation(series, "321.5", known_offset_hours=96)]) == 1
    # ...then an ALFRED backfill inserts the true first print, earlier and
    # with the same value: it precedes the forward row, so it must be kept.
    assert r.insert_many([observation(series, "321.5")]) == 1
    chain = r.known_before(series, T0 + timedelta(days=30))
    assert len(chain) == 2
    assert chain[0].known_at == T0


def test_value_can_revert_to_an_earlier_vintage(repo):
    r, series = repo
    r.insert_many([observation(series, "321.5")])
    r.insert_many([observation(series, "321.7", known_offset_hours=24)])
    # A revision back to the original value differs from its immediate
    # predecessor (321.7), so it is a real vintage.
    assert r.insert_many([observation(series, "321.5", known_offset_hours=48)]) == 1
    chain = r.known_before(series, T0 + timedelta(days=30))
    assert [str(o.value) for o in chain] == ["321.5", "321.7", "321.5"]


def test_visibility_cutoff_hides_future_vintages(repo):
    r, series = repo
    r.insert_many([observation(series, "321.5")])
    r.insert_many([observation(series, "321.7", known_offset_hours=24)])
    visible = r.known_before(series, T0 + timedelta(hours=1))
    assert [o.value for o in visible] == [Decimal("321.5")]