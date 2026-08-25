"""StoredFeatureSource: the PIT series in, a feature snapshot out."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading.data.features import StoredFeatureSource
from trading.data.macro.registry import US_TREASURY_2Y_YIELD
from trading.domain.economic import EconomicObservation
from trading.domain.event import EventEnvelope
from trading.intelligence import features as f
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

WEIGHTS = InterventionRiskConfig(
    version="test",
    weights={"days_since_intervention": 0.6, "verification_state": 0.4},
)


class FakeObservations:
    def __init__(self, observations=()):
        self._observations = list(observations)

    def known_before(self, series, t):
        return [
            o
            for o in self._observations
            if o.series == series and o.known_at <= t
        ]


class FakeEvents:
    def __init__(self, events=()):
        self._events = list(events)

    def known_before(self, t, event_type=None):
        return [
            e
            for e in self._events
            if e.known_at <= t and (event_type is None or e.event_type == event_type)
        ]


def observation(day: date, value: str) -> EconomicObservation:
    known = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=23)
    return EconomicObservation(
        observation_id=uuid4(),
        series=US_TREASURY_2Y_YIELD,
        observation_period=day.isoformat(),
        value=Decimal(value),
        unit="percent",
        source="ALFRED",
        retrieved_at=known,
        known_at=known,
    )


def score_event(event_type: str, score: float, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        source="TEST",
        payload={"score": score},
        retrieved_at=known_at,
        known_at=known_at,
    )


def intervention_event(action_date: date, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type="INTERVENTION_REPORTED",
        source="TEST",
        payload={"action_date": action_date.isoformat()},
        retrieved_at=known_at,
        known_at=known_at,
    )


def make_source(observations=(), events=(), store=None):
    return StoredFeatureSource(
        FakeObservations(observations),
        FakeEvents(events),
        WEIGHTS,
        store if store is not None else InMemoryFeatureStore(),
    )


def test_snapshot_composes_all_producible_features():
    days = [date(2026, 8, 18) + timedelta(days=i) for i in range(6)]
    values = ["3.50", "3.52", "3.55", "3.53", "3.60", "3.66"]
    source = make_source(
        observations=[observation(d, v) for d, v in zip(days, values, strict=True)],
        events=[
            score_event("BOJ_POLICY_SHIFT_SCORE", 0.5, NOW - timedelta(days=10)),
            score_event("FED_POLICY_SHIFT_SCORE", -1.0, NOW - timedelta(days=12)),
            intervention_event(date(2026, 8, 20), NOW - timedelta(days=3)),
        ],
    )

    snapshot = source.snapshot(NOW)

    assert snapshot[f.US2Y_LEVEL] == 3.66
    assert snapshot[f.US2Y_CHANGE_1D] == pytest.approx(0.06)
    assert snapshot[f.US2Y_CHANGE_5D] == pytest.approx(0.16)
    assert snapshot[f.BOJ_POLICY_SHIFT_SCORE] == 0.5
    assert snapshot[f.FED_POLICY_SHIFT_SCORE] == -1.0
    assert 0.0 < snapshot[f.INTERVENTION_RISK] <= 1.0


def test_missing_inputs_leave_the_feature_absent_not_zero():
    snapshot = make_source().snapshot(NOW)

    assert snapshot == {}


def test_no_recent_intervention_is_absence_of_evidence():
    # An intervention outside the recency window contributes nothing, and
    # "nothing" must read as missing — a 0.0 would tell strategies the risk is
    # known to be low.
    source = make_source(
        events=[intervention_event(date(2026, 1, 5), NOW - timedelta(days=200))]
    )

    assert f.INTERVENTION_RISK not in source.snapshot(NOW)


def test_visibility_respects_the_reading_clock():
    # A score stored with a future known_at is tomorrow's knowledge; today's
    # snapshot must not contain it.
    source = make_source(
        events=[score_event("FED_POLICY_SHIFT_SCORE", 2.0, NOW + timedelta(hours=1))]
    )

    assert f.FED_POLICY_SHIFT_SCORE not in source.snapshot(NOW)


def test_refresh_removes_what_is_no_longer_computable():
    store = InMemoryFeatureStore()
    events = FakeEvents(
        [score_event("BOJ_POLICY_SHIFT_SCORE", 0.5, NOW - timedelta(days=1))]
    )
    source = StoredFeatureSource(FakeObservations(), events, WEIGHTS, store)

    source.refresh(NOW)
    assert store.get(f.BOJ_POLICY_SHIFT_SCORE) == 0.5

    events._events.clear()
    source.refresh(NOW)
    assert store.get(f.BOJ_POLICY_SHIFT_SCORE) is None
