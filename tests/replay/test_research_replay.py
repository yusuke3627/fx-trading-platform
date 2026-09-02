"""An archived period replays on the reconstructed axis, not on ingestion.

Every tick of the dataset carries one shared ingestion instant — the shape a
backfill run produces — so a replay ordered on stored received_at would see
the whole period at once, with every score already visible. The runner's
rewrite (ADR-014) has to restore the intra-period structure: a score whose
known_at falls mid-period is invisible to evaluations before its instant.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from tests.support import FakeEventRepository, FakeObservationRepository, usdjpy_spec
from trading.backtest.costs import CostModel
from trading.backtest.engine import BacktestEngine
from trading.backtest.research import broker_label_to_known, reconstructed
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

# Broker-clock start of the archived period. The server's wall clock is New
# York's plus ANCHOR; in January that is UTC+2, so the same instant on the
# known-time axis is broker_label_to_known(BROKER_START, ANCHOR).
BROKER_START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
ANCHOR = timedelta(hours=7)
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
    real_start = broker_label_to_known(BROKER_START, ANCHOR)
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

    engine.run(reconstructed(archived_ticks(600), ANCHOR))

    assert len(readings) == 600
    assert set(readings[:300]) == {-1.0}
    assert set(readings[300:]) == {2.0}


def test_warmup_ticks_build_state_but_are_never_evaluated():
    # A research run reads lead-in ticks ahead of its period; the strategy
    # must not be asked during them — the first evaluation is the period's
    # opening instant, with bar state already populated.
    real_start = broker_label_to_known(BROKER_START, ANCHOR)
    evaluate_from = real_start + timedelta(seconds=200)

    readings: list[float | None] = []
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
        evaluate_from=evaluate_from,
    )

    engine.run(reconstructed(archived_ticks(600), ANCHOR))

    # Ticks 0..199 are warm-up (known times before evaluate_from); the tick
    # AT the boundary already evaluates.
    assert len(readings) == 400


class WideTickWindowStrategy(Strategy):
    """Requests a tick window far beyond the default retention horizon."""

    strategy_id = "wide_tick_window_probe"
    strategy_version = "0.0.1"
    horizon = StrategyHorizon.SCALP

    @classmethod
    def tick_window_seconds(cls, config) -> float:
        return float(config.params_for("").param("window_seconds", 0))

    def __init__(self, counts: list[int]) -> None:
        self._counts = counts

    async def on_event(self, event, context):
        window = float(context.config.params_for("USDJPY").param("window_seconds", 0))
        self._counts.append(len(context.market.ticks("USDJPY", window)))
        return []


def test_tick_retention_follows_the_strategy_configuration():
    # 7200s is beyond the default 3600s horizon; the engine must size
    # retention from the strategy's declared window instead of refusing the
    # read mid-replay.
    counts: list[int] = []
    engine = BacktestEngine(
        risk_config=RiskConfig(
            trading_enabled=False, event_mode_default=EventRiskMode.NORMAL
        ),
        spec=usdjpy_spec(),
        costs=CostModel(),
        seed=7,
        strategy_factory=lambda: WideTickWindowStrategy(counts),
        strategy_config=StrategyConfig(
            strategy_id=WideTickWindowStrategy.strategy_id,
            enabled=True,
            instruments=["USDJPY"],
            parameters={"window_seconds": 7200},
        ),
    )

    engine.run(reconstructed(archived_ticks(600), ANCHOR))

    assert counts[-1] == 600
