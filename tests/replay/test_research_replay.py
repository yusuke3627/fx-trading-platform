"""An archived period replays on the reconstructed axis, not on ingestion.

Every tick of the dataset carries one shared ingestion instant — the shape a
backfill run produces — so a replay ordered on stored received_at would see
the whole period at once, with every score already visible. The runner's
rewrite (ADR-007) has to restore the intra-period structure: a score whose
known_at falls mid-period is invisible to evaluations before its instant.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from tests.support import FakeEventRepository, FakeObservationRepository, usdjpy_spec
from trading.backtest.costs import CostModel
from trading.backtest.engine import BacktestEngine
from trading.backtest.research import reconstructed
from trading.data.features import ReplayFeatureTimeline, StoredFeatureSource
from trading.data.policy.scoring import SCORING_VERSION
from trading.domain.event import EventEnvelope
from trading.domain.market import Tick
from trading.domain.risk import EventRiskMode
from trading.intelligence import features as f
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig
from trading.risk.engine import RiskConfig
from trading.strategy.base import Strategy, StrategyConfig, StrategyHorizon

# Broker-clock start of the archived period; the broker runs 3h ahead of real
# UTC, so the same instant on the known-time axis is BROKER_START - OFFSET.
BROKER_START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
OFFSET = timedelta(hours=3)
INGESTED_AT = BROKER_START + timedelta(days=100)


class FeatureProbeStrategy(Strategy):
    strategy_id = "feature_probe"
    strategy_version = "0.0.1"
    horizon = StrategyHorizon.INTRADAY

    def __init__(self, readings: list[float | None]) -> None:
        self._readings = readings

    async def on_event(self, event, context):
        self._readings.append(context.features.get(f.FED_POLICY_SHIFT_SCORE))
        return []


def score_event(score: float, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type="FED_POLICY_SHIFT_SCORE",
        source="TEST",
        payload={"score": score, "scoring_version": SCORING_VERSION},
        retrieved_at=known_at,
        known_at=known_at,
    )


def archived_ticks(count: int) -> list[Tick]:
    return [
        Tick(
            symbol="USDJPY",
            bid=Decimal("157.000"),
            ask=Decimal("157.005"),
            time=BROKER_START + timedelta(seconds=i),
            received_at=INGESTED_AT,
        )
        for i in range(count)
    ]


def test_a_mid_period_score_stays_invisible_until_its_reconstructed_instant():
    real_start = BROKER_START - OFFSET
    revision_at = real_start + timedelta(seconds=300)
    events = [
        score_event(-1.0, real_start - timedelta(days=2)),
        score_event(2.0, revision_at),
    ]

    readings: list[float | None] = []
    source = StoredFeatureSource(
        FakeObservationRepository(),
        FakeEventRepository(events),
        InterventionRiskConfig(version="test", weights={}),
        InMemoryFeatureStore(),
    )
    timeline = ReplayFeatureTimeline(
        source,
        source.change_instants(real_start, real_start + timedelta(seconds=600)),
    )
    engine = BacktestEngine(
        risk_config=RiskConfig(
            trading_enabled=False, event_mode_default=EventRiskMode.NORMAL
        ),
        spec=usdjpy_spec(),
        costs=CostModel(),
        seed=7,
        strategy_factory=lambda: FeatureProbeStrategy(readings),
        strategy_config=StrategyConfig(
            strategy_id=FeatureProbeStrategy.strategy_id,
            enabled=True,
            instruments=["USDJPY"],
        ),
        features=timeline,
    )

    engine.run(reconstructed(archived_ticks(600), OFFSET))

    assert len(readings) == 600
    assert set(readings[:300]) == {-1.0}
    assert set(readings[300:]) == {2.0}
