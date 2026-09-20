"""実戦略の PIT 入力から Signal / Intent までを Replay と Shadow で照合する。

保存境界だけをメモリ化し、戦略・特徴量・指標・Portfolio・Risk は本実装を通す。
比較する口座は同一残高の flat book。Replay の注文は simulator で全拒否し、
Shadow 固有の発注・照合 gate は閉じたままにする。約定の一致は対象外。
単一戦略の LONG が発火する条件を検証し、未発火の SHORT や既存保有へ一般化しない。
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from tests.support import (
    T0,
    FakeAccountSnapshotRepository,
    FakeBarRepository,
    FakeDecisionRepository,
    FakeEventRepository,
    FakeObservationRepository,
    FakeTickRepository,
    FixedClock,
    at,
    make_snapshot,
    make_tick,
    usdjpy_spec,
)
from trading.backtest.costs import CostModel
from trading.backtest.engine import BacktestEngine
from trading.data.features import ReplayFeatureTimeline, StoredFeatureSource
from trading.data.market.bars import BarBuilder
from trading.data.market.stored import StoredMarketData
from trading.data.policy.scoring import SCORING_VERSION
from trading.domain.account import AccountMode
from trading.domain.event import EventEnvelope
from trading.domain.intent import PositionIntent
from trading.domain.market import Tick
from trading.domain.risk import EventRiskMode, RiskDecision
from trading.indicators import IndicatorService
from trading.intelligence import features as f
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.intervention import InterventionRiskConfig
from trading.intelligence.regime import (
    RuleBasedCurrencyRegimeService,
    RuleBasedRegimeService,
)
from trading.live.clock import CycleClock
from trading.live.shadow import ShadowInstrument, ShadowRunner
from trading.portfolio.arbitrator import ArbitratorConfig, PortfolioArbitrator
from trading.portfolio.exposure import CurrencyExposureService
from trading.portfolio.manager import PortfolioManager, SizingInput
from trading.portfolio.virtual_ledger import VirtualPositionLedger
from trading.risk.conversion import MarketQuoteConversionService
from trading.risk.engine import PreTradeContext, RiskConfig, RiskEngine
from trading.risk.event_risk import EventRiskCalendar
from trading.runner import StrategyBinding, StrategyRunner
from trading.strategy.base import StrategyConfig, StrategyContext, StrategyStatus, TimeframeMap
from trading.strategy.swing.monetary_policy_convergence import MonetaryPolicyConvergenceStrategy

EVALUATE_FROM = at(minutes=50)
CUTOFF = at(minutes=55)
REVISION_AT = at(minutes=60)
ACCOUNT = "Example-Broker Demo:10000001"
FEATURE_NAMES = (f.FED_POLICY_SHIFT_SCORE, f.BOJ_POLICY_SHIFT_SCORE, f.INTERVENTION_RISK)


@dataclass(frozen=True)
class Evaluation:
    at: datetime
    features: tuple[float | None, ...]
    indicators: tuple[float | None, ...]
    signals: list[dict[str, Any]]


@dataclass
class DecisionTrace:
    evaluations: list[Evaluation] = field(default_factory=list)
    intents: list[dict[str, Any]] = field(default_factory=list)
    sizing: list[SizingInput] = field(default_factory=list)
    risks: list[tuple[PositionIntent, RiskDecision, PreTradeContext]] = field(default_factory=list)

    def prefix(self, until: datetime) -> tuple[list[Evaluation], list[dict[str, Any]]]:
        return (
            [value for value in self.evaluations if value.at <= until],
            [value for value in self.intents if value["generated_at"] <= until],
        )


def record_decisions(patch: pytest.MonkeyPatch) -> DecisionTrace:
    """本実装の返値を観測し、比較では実行ごとに発行する UUID だけを除く。"""
    trace = DecisionTrace()
    on_event = MonetaryPolicyConvergenceStrategy.on_event
    make_intents = PortfolioManager.intents_from_signal
    evaluate_risk = RiskEngine.evaluate

    async def observed_event(self, event, context):
        signals = await on_event(self, event, context)
        trace.evaluations.append(Evaluation(
            at=context.clock.now(),
            features=tuple(context.features.get(name) for name in FEATURE_NAMES),
            indicators=(
                context.indicators.atr("USDJPY", "1m", 14),
                context.indicators.ema("USDJPY", "1m", 20),
                context.indicators.ema("USDJPY", "1m", 50),
            ),
            signals=[signal.model_dump(exclude={"signal_id"}) for signal in signals],
        ))
        return signals

    def observed_intents(self, signal, sizing):
        intents = make_intents(self, signal, sizing)
        trace.sizing.append(sizing)
        trace.intents.extend(intent.model_dump(exclude={"intent_id"}) for intent in intents)
        return intents

    def observed_risk(self, intent, context):
        decision = evaluate_risk(self, intent, context)
        assert decision.intent_id == intent.intent_id
        trace.risks.append((intent, decision, context))
        return decision

    patch.setattr(MonetaryPolicyConvergenceStrategy, "on_event", observed_event)
    patch.setattr(PortfolioManager, "intents_from_signal", observed_intents)
    patch.setattr(RiskEngine, "evaluate", observed_risk)
    return trace


def score_event(kind: str, score: float, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=kind,
        source="EXAMPLE_POLICY_FEED",
        payload={"score": score, "scoring_version": SCORING_VERSION},
        # 改訂も同じ過去の公表を指す。公開日ではなく取得した時刻で可視性が決まる。
        effective_at=T0,
        published_at=T0,
        retrieved_at=known_at,
        known_at=known_at,
    )


@pytest.fixture
def dataset() -> tuple[list[Tick], list[EventEnvelope]]:
    ticks = []
    for index in range(141):
        bid = Decimal(150) + Decimal(index) / 100
        ticks.append(make_tick(
            str(bid), str(bid + Decimal("0.004")),
            time=at(hours=3, seconds=index * 30),
            received_at=at(seconds=index * 30),
        ))
    return ticks, [
        score_event("FED_POLICY_SHIFT_SCORE", 1.0, T0),
        score_event("BOJ_POLICY_SHIFT_SCORE", -1.0, T0),
        score_event("FED_POLICY_SHIFT_SCORE", -1.0, REVISION_AT),
    ]


class LeakingEventRepository(FakeEventRepository):
    """陽性対照: 保存境界が未来行を混ぜる不具合を意図的に作る。"""

    def known_before(self, t, event_type=None, since=None):
        return super().known_before(at(days=1), event_type, since)


def run_path(
    path: str,
    dataset: tuple[list[Tick], list[EventEnvelope]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    leak_events: bool = False,
) -> DecisionTrace:
    ticks, events = dataset
    config = StrategyConfig(
        strategy_id=MonetaryPolicyConvergenceStrategy.strategy_id,
        enabled=True,
        status=StrategyStatus.SHADOW,
        instruments=["USDJPY"],
        timeframes=TimeframeMap(trigger="1m", trend="1m"),
    )
    risk = RiskConfig(
        trading_enabled=True,
        event_mode_default=EventRiskMode.NORMAL,
        absolute_max_spread_pips={"USDJPY": Decimal(10)},
        max_units_per_symbol={"USDJPY": 10000},
    )
    repo = LeakingEventRepository(events) if leak_events else FakeEventRepository(events)
    source = StoredFeatureSource(
        FakeObservationRepository(), repo,
        InterventionRiskConfig(version="test", weights={}), InMemoryFeatureStore(),
    )
    calendar = EventRiskCalendar([], (at(days=-1), at(days=1)))
    with monkeypatch.context() as patch:
        trace = record_decisions(patch)
        if path == "replay":
            engine = BacktestEngine(
                risk_config=risk, spec=usdjpy_spec(), seed=7,
                costs=CostModel(reject_probability=1.0),
                strategy_factory=MonetaryPolicyConvergenceStrategy,
                strategy_config=config,
                features=ReplayFeatureTimeline(source, [event.known_at for event in events]),
                event_risk=calendar, evaluate_from=EVALUATE_FROM,
            )
            result = engine.run(ticks)
            assert result.fills == []
        else:
            run_shadow(ticks, config, risk, source, calendar)
    return trace


def run_shadow(
    ticks: list[Tick],
    config: StrategyConfig,
    risk: RiskConfig,
    source: StoredFeatureSource,
    calendar: EventRiskCalendar,
) -> None:
    spec = usdjpy_spec()
    source_clock = FixedClock(EVALUATE_FROM)
    clock = CycleClock(source_clock)
    builder = BarBuilder(spec.symbol, "1m")
    # 未来の完成足まで保存済みの条件で、StoredMarketData の PIT 読取を通す。
    bars = [bar for tick in ticks if (bar := builder.on_tick(tick)) is not None]
    market = StoredMarketData(
        FakeTickRepository(ticks), FakeBarRepository(bars), clock, {spec.symbol: spec}
    )
    ledger = VirtualPositionLedger(clock)
    conversion = MarketQuoteConversionService(market, [spec])
    snapshots = FakeAccountSnapshotRepository()
    decisions = FakeDecisionRepository()
    context = StrategyContext(
        clock=clock, market=market, indicators=IndicatorService(market),
        features=source.store, regime=RuleBasedRegimeService(source.store),
        currency_states=source.currency_states,
        currency_regime=RuleBasedCurrencyRegimeService(source.store),
        portfolio=ledger, config=config,
    )
    runner = ShadowRunner(
        runner=StrategyRunner([StrategyBinding(MonetaryPolicyConvergenceStrategy(), context)]),
        portfolio=PortfolioManager(ledger, clock, conversion), ledger=ledger,
        risk=RiskEngine(risk, clock, conversion), risk_config=risk, market=market,
        snapshots=snapshots, decisions=decisions, event_risk=calendar, clock=clock,
        account_id=ACCOUNT, account_mode=AccountMode.HEDGING,
        instruments=[ShadowInstrument(spec, trading_enabled=True)],
        exposure=CurrencyExposureService(conversion),
        arbitrator=PortfolioArbitrator(ArbitratorConfig()), features=source,
    )
    for tick in ticks:
        if tick.known_time < EVALUATE_FROM:
            continue
        source_clock.advance(seconds=(tick.known_time - source_clock.now()).total_seconds())
        snapshots.insert(ACCOUNT, make_snapshot("1000000", observed_at=tick.known_time))
        cycle = runner.evaluate_once()
        assert cycle.blocked == {}
        assert all(item.decision is not None for item in cycle.decisions)
    assert ledger.open_positions() == []


def assert_same_prefix(
    left: DecisionTrace, right: DecisionTrace, until: datetime = CUTOFF
) -> None:
    assert left.prefix(until) == right.prefix(until)


@pytest.mark.parametrize("path", ["replay", "shadow"])
@pytest.mark.parametrize("mutation", ["delete", "change"])
def test_future_prices_cannot_rewrite_prior_decisions(path, mutation, dataset, monkeypatch):
    ticks, events = dataset
    changed = [tick for tick in ticks if tick.known_time <= CUTOFF]
    if mutation == "change":
        changed += [
            tick.model_copy(update={"bid": tick.bid - 5, "ask": tick.ask - 5})
            for tick in ticks if tick.known_time > CUTOFF
        ]
    baseline = run_path(path, dataset, monkeypatch)
    altered = run_path(path, (changed, events), monkeypatch)

    assert baseline.prefix(CUTOFF)[1], "空の Intent 同士では比較にならない"
    assert_same_prefix(baseline, altered)
    assert baseline.evaluations != altered.evaluations
    assert baseline.intents != altered.intents


@pytest.mark.parametrize("path", ["replay", "shadow"])
@pytest.mark.parametrize("mutation", ["delete", "change"])
def test_late_revision_cannot_rewrite_prior_decisions(path, mutation, dataset, monkeypatch):
    ticks, events = dataset
    changed = events[:2]
    if mutation == "change":
        changed += [score_event("FED_POLICY_SHIFT_SCORE", 2.0, REVISION_AT)]
    baseline = run_path(path, dataset, monkeypatch)
    altered = run_path(path, (ticks, changed), monkeypatch)

    assert baseline.prefix(CUTOFF)[1]
    assert_same_prefix(baseline, altered, at(minutes=59, seconds=30))
    at_revision = next(value for value in baseline.evaluations if value.at == REVISION_AT)
    assert at_revision.features[0] == -1.0
    assert at_revision.signals == []
    assert any(intent["generated_at"] == REVISION_AT for intent in altered.intents)


@pytest.mark.parametrize("path", ["replay", "shadow"])
def test_prefix_comparison_detects_deliberate_lookahead(path, dataset, monkeypatch):
    ticks, events = dataset
    leaked = run_path(path, dataset, monkeypatch, leak_events=True)
    removed_revision = run_path(path, (ticks, events[:2]), monkeypatch, leak_events=True)

    assert leaked.prefix(CUTOFF)[1] == []
    assert removed_revision.prefix(CUTOFF)[1]
    with pytest.raises(AssertionError):
        assert_same_prefix(leaked, removed_revision)


def test_shadow_and_replay_agree_before_execution_gates(dataset, monkeypatch):
    replay = run_path("replay", dataset, monkeypatch)
    shadow = run_path("shadow", dataset, monkeypatch)

    assert len(replay.intents) >= 5
    assert replay.evaluations == shadow.evaluations
    assert [value.at for value in replay.evaluations] == [
        tick.known_time for tick in dataset[0] if tick.known_time >= EVALUATE_FROM
    ]
    assert replay.intents == shadow.intents
    assert replay.sizing == shadow.sizing
    assert len(replay.risks) == len(shadow.risks) == len(replay.intents)
    for (intent, approved, replay_context), (_, rejected, shadow_context) in zip(
        replay.risks, shadow.risks, strict=True
    ):
        assert replay_context.account == shadow_context.account
        assert replay_context.account.equity == Decimal(1000000)
        assert replay_context.symbol_open_positions_count == 0
        assert shadow_context.symbol_open_positions_count == 0
        assert intent.target_quantity is not None and intent.target_quantity > 0
        assert approved.approved
        assert approved.approved_quantity == intent.target_quantity
        assert not rejected.approved
        assert rejected.approved_quantity is None
        assert set(rejected.reject_codes) == {"EXECUTION_ENABLED", "ACCOUNT_RECONCILED"}
