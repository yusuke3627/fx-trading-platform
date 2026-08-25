"""Research runner pieces: the replay axis rewrite and the change schedule."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from tests.support import FakeEventRepository, FakeObservationRepository
from trading.backtest.research import reconstructed
from trading.data.features import US2Y_VINTAGE_LOOKBACK, StoredFeatureSource
from trading.data.intervention.features import RECENCY_WINDOW_DAYS
from trading.data.macro.registry import US_TREASURY_2Y_YIELD
from trading.domain.economic import EconomicObservation
from trading.domain.event import EventEnvelope
from trading.domain.market import Tick
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig

START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
END = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)


def tick(at: datetime, ingested_at: datetime) -> Tick:
    return Tick(
        symbol="USDJPY",
        bid=Decimal("157.000"),
        ask=Decimal("157.005"),
        time=at,
        received_at=ingested_at,
    )


def observation(known_at: datetime) -> EconomicObservation:
    return EconomicObservation(
        observation_id=uuid4(),
        series=US_TREASURY_2Y_YIELD,
        observation_period=known_at.date().isoformat(),
        value=Decimal("3.50"),
        unit="percent",
        source="ALFRED",
        retrieved_at=known_at,
        known_at=known_at,
    )


def event(event_type: str, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        source="TEST",
        payload={"action_date": known_at.date().isoformat()},
        retrieved_at=known_at,
        known_at=known_at,
    )


def make_source(observations=(), events=()) -> StoredFeatureSource:
    return StoredFeatureSource(
        FakeObservationRepository(observations),
        FakeEventRepository(events),
        InterventionRiskConfig(version="test", weights={}),
        InMemoryFeatureStore(),
    )


def test_reconstructed_rewrites_known_time_from_the_broker_stamp():
    # An archived tick carries the backfill run's wall clock as received_at —
    # far in the tick's own future. The replay axis must come from the broker
    # stamp instead.
    ingested = START + timedelta(days=400)
    ticks = [tick(START + timedelta(seconds=i), ingested) for i in range(3)]

    rewritten = reconstructed(ticks, timedelta(hours=3))

    assert [t.known_time for t in rewritten] == [
        START - timedelta(hours=3) + timedelta(seconds=i) for i in range(3)
    ]
    # The broker axis itself is untouched: bars keep folding event_time.
    assert [t.time for t in rewritten] == [t.time for t in ticks]
    assert rewritten[0].bid == ticks[0].bid


def test_change_instants_mirror_the_snapshot_windows():
    inside_lookback = observation(START - US2Y_VINTAGE_LOOKBACK + timedelta(days=1))
    during = observation(START + timedelta(days=5))
    beyond_lookback = observation(START - US2Y_VINTAGE_LOOKBACK - timedelta(days=10))

    # The policy read is unbounded, so an old score's arrival is an instant
    # even when only its lookback expiry could fall inside the replay.
    old_policy = event("FED_POLICY_SHIFT_SCORE", START - timedelta(days=200))

    recent_intervention = event("INTERVENTION_REPORTED", START + timedelta(days=2))
    stale_intervention = event(
        "INTERVENTION_REPORTED",
        START - timedelta(days=RECENCY_WINDOW_DAYS + 5),
    )

    source = make_source(
        observations=[during, beyond_lookback, inside_lookback],
        events=[old_policy, recent_intervention, stale_intervention],
    )

    instants = source.change_instants(START, END)

    assert instants == sorted(
        [
            old_policy.known_at,
            inside_lookback.known_at,
            recent_intervention.known_at,
            during.known_at,
        ]
    )


def test_dataset_fingerprint_follows_content_not_row_identity():
    # The factories mint fresh UUIDs per row, so equality across two builds
    # proves the hash covers content, the way a re-ingested archive repeats it.
    rows = {
        "observations": [observation(START + timedelta(days=1))],
        "events": [event("FED_POLICY_SHIFT_SCORE", START - timedelta(days=30))],
    }
    first = make_source(**rows).dataset_fingerprint(START, END)

    assert make_source(**rows).dataset_fingerprint(START, END) == first


def test_dataset_fingerprint_changes_only_with_rows_a_replay_can_read():
    observations = [observation(START + timedelta(days=1))]
    base = make_source(observations=observations).dataset_fingerprint(START, END)

    grown = make_source(
        observations=observations,
        events=[event("INTERVENTION_REPORTED", START + timedelta(days=2))],
    ).dataset_fingerprint(START, END)
    out_of_window = make_source(
        observations=[
            *observations,
            observation(START - US2Y_VINTAGE_LOOKBACK - timedelta(days=10)),
        ],
    ).dataset_fingerprint(START, END)

    assert grown != base
    assert out_of_window == base
