"""Features inside a backtest: what a strategy reads follows the replay clock.

The parity requirement in one test: a score whose known_at falls mid-dataset
must be invisible to every evaluation before that instant and visible to every
one after it, with no look-ahead and no lag beyond the tick that crosses it.
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tests.support import FakeEventRepository, FakeObservationRepository, usdjpy_spec
from trading.backtest.costs import CostModel
from trading.backtest.data import synthetic_ticks
from trading.backtest.engine import BacktestEngine
from trading.data.features import ReplayFeatureTimeline, StoredFeatureSource
from trading.data.policy.scoring import SCORING_VERSION
from trading.domain.event import EventEnvelope
from trading.domain.risk import EventRiskMode
from trading.intelligence import features as f
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig
from trading.risk.engine import RiskConfig
from trading.strategy.base import Strategy, StrategyConfig, StrategyHorizon

START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


class FeatureProbeStrategy(Strategy):
    """Records what the store answers at every evaluation; trades nothing."""

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


def run_probe(events) -> list[float | None]:
    readings: list[float | None] = []
    source = StoredFeatureSource(
        FakeObservationRepository(),
        FakeEventRepository(events),
        InterventionRiskConfig(version="test", weights={}),
        InMemoryFeatureStore(),
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
        features=ReplayFeatureTimeline(source, [e.known_at for e in events]),
    )
    ticks = synthetic_ticks(spec=usdjpy_spec(), start=START, count=600, seed=7)
    engine.run(ticks)
    return readings


def test_a_score_becomes_visible_at_its_known_at_and_not_before():
    # One tick per second from START: the 300th evaluation happens at
    # START+300s, which is where the score lands.
    revision_at = START + timedelta(seconds=300)
    readings = run_probe(
        [score_event(-1.0, START - timedelta(days=2)), score_event(2.0, revision_at)]
    )

    assert len(readings) == 600
    # History before the replay is visible from the very first evaluation.
    assert set(readings[:300]) == {-1.0}
    assert set(readings[300:]) == {2.0}


def test_no_events_means_the_store_stays_empty_for_the_whole_replay():
    assert set(run_probe([])) == {None}
