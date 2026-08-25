"""ReplayFeatureTimeline: replay sees the same features live would have.

The timeline exists so a replay does not query per tick; these tests pin that
the economy never changes what a strategy reads — every stepped state equals
what a live-style refresh at the same instant computes.
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tests.support import FakeEventRepository, FakeObservationRepository
from trading.data.features import ReplayFeatureTimeline, StoredFeatureSource
from trading.data.policy.scoring import SCORING_VERSION
from trading.domain.event import EventEnvelope
from trading.intelligence import features as f
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig

T0 = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)

WEIGHTS = InterventionRiskConfig(
    version="test",
    weights={"days_since_intervention": 1.0},
)


def score_event(score: float, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type="FED_POLICY_SHIFT_SCORE",
        source="TEST",
        payload={"score": score, "scoring_version": SCORING_VERSION},
        retrieved_at=known_at,
        known_at=known_at,
    )


def intervention_event(known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type="INTERVENTION_REPORTED",
        source="TEST",
        payload={"action_date": known_at.date().isoformat()},
        retrieved_at=known_at,
        known_at=known_at,
    )


def timeline_over(events, changes):
    source = StoredFeatureSource(
        FakeObservationRepository(),
        FakeEventRepository(events),
        WEIGHTS,
        InMemoryFeatureStore(),
    )
    return ReplayFeatureTimeline(source, changes)


def test_reset_makes_history_before_the_start_visible_immediately():
    earlier = score_event(-1.0, T0 - timedelta(days=10))
    timeline = timeline_over([earlier], changes=[earlier.known_at])

    timeline.reset(T0)

    assert timeline.store.get(f.FED_POLICY_SHIFT_SCORE) == -1.0


def test_a_change_instant_is_applied_on_the_first_tick_at_or_past_it():
    change_at = T0 + timedelta(hours=6)
    timeline = timeline_over([score_event(2.0, change_at)], changes=[change_at])
    timeline.reset(T0)

    timeline.advance(change_at - timedelta(seconds=1))
    assert timeline.store.get(f.FED_POLICY_SHIFT_SCORE) is None

    timeline.advance(change_at)
    assert timeline.store.get(f.FED_POLICY_SHIFT_SCORE) == 2.0


def test_date_boundaries_refresh_without_any_new_row():
    # Intervention risk decays on date arithmetic: with only
    # days_since_intervention weighted, every midnight lowers the score — a
    # change no row-known_at announces, so only the date-boundary refresh can
    # deliver it.
    event = intervention_event(T0 - timedelta(hours=12))
    timeline = timeline_over([event], changes=[event.known_at])
    timeline.reset(T0)
    day_zero = timeline.store.get(f.INTERVENTION_RISK)

    timeline.advance(T0 + timedelta(days=1, minutes=5))

    day_one = timeline.store.get(f.INTERVENTION_RISK)
    assert day_zero is not None
    assert day_one is not None and day_one < day_zero


def test_stepping_matches_a_live_refresh_at_every_sampled_instant():
    events = [
        score_event(-1.0, T0 - timedelta(days=3)),
        score_event(0.5, T0 + timedelta(hours=2)),
        score_event(1.5, T0 + timedelta(days=1, hours=1)),
        intervention_event(T0 + timedelta(hours=8)),
    ]
    changes = [e.known_at for e in events]
    timeline = timeline_over(events, changes)
    timeline.reset(T0)

    # An independent source refreshed the way live shadow does it.
    live = StoredFeatureSource(
        FakeObservationRepository(), FakeEventRepository(events), WEIGHTS,
        InMemoryFeatureStore(),
    )

    instant = T0
    while instant <= T0 + timedelta(days=2):
        timeline.advance(instant)
        assert timeline.store.get(f.FED_POLICY_SHIFT_SCORE) == live.snapshot(instant).get(
            f.FED_POLICY_SHIFT_SCORE
        ), instant
        assert timeline.store.get(f.INTERVENTION_RISK) == live.snapshot(instant).get(
            f.INTERVENTION_RISK
        ), instant
        instant += timedelta(minutes=37)


def test_reset_rewinds_a_used_timeline():
    change_at = T0 + timedelta(hours=6)
    timeline = timeline_over([score_event(2.0, change_at)], changes=[change_at])
    timeline.reset(T0)
    timeline.advance(T0 + timedelta(days=1))
    assert timeline.store.get(f.FED_POLICY_SHIFT_SCORE) == 2.0

    timeline.reset(T0)

    assert timeline.store.get(f.FED_POLICY_SHIFT_SCORE) is None
    timeline.advance(change_at)
    assert timeline.store.get(f.FED_POLICY_SHIFT_SCORE) == 2.0
