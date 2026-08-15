"""Vertical-slice acceptance tests.

The first acceptance criterion of the backtest system is NOT profitability:

1. Same dataset + config + seed -> identical signals, fills, PnL, metrics.
2. Worse execution costs (spread/slippage stress) -> strictly worse net PnL.
3. The full order lifecycle (OPEN -> ticket-referenced CLOSE -> reversal
   OPEN, protection fills) flows through Risk -> OMS -> Simulator -> Ledger.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.support import usdjpy_spec
from trading.backtest.costs import STRESS_SCENARIOS, CostModel
from trading.backtest.data import dataset_hash, synthetic_ticks
from trading.backtest.engine import BacktestEngine, BacktestResult, ScriptedStrategy
from trading.domain.position import PositionDirection
from trading.domain.risk import EventRiskMode
from trading.risk.engine import RiskConfig
from trading.strategy.base import StrategyConfig

DATASET_START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def slice_risk_config() -> RiskConfig:
    # Halt thresholds are opened up: these tests assert engine mechanics,
    # not the production loss limits (covered by risk-engine tests).
    return RiskConfig(
        trading_enabled=True,
        max_units_per_symbol={"USDJPY": 10000},
        max_risk_per_trade_pct=Decimal("1.0"),
        daily_loss_halt_pct=Decimal(50),
        rolling_24h_loss_halt_pct=Decimal(50),
        high_water_mark_drawdown_halt_pct=Decimal(50),
        event_mode_default=EventRiskMode.NORMAL,
    )


def build_engine(
    costs: CostModel,
    *,
    seed: int = 7,
    plan: dict[int, PositionDirection] | None = None,
    stop_distance_pips: Decimal = Decimal(200),
    risk_config: RiskConfig | None = None,
) -> BacktestEngine:
    resolved_plan = (
        plan
        if plan is not None
        else {300: PositionDirection.LONG, 1200: PositionDirection.SHORT}
    )
    return BacktestEngine(
        risk_config=risk_config if risk_config is not None else slice_risk_config(),
        spec=usdjpy_spec(),
        costs=costs,
        seed=seed,
        strategy_factory=lambda: ScriptedStrategy(
            resolved_plan, stop_distance_pips=stop_distance_pips
        ),
        strategy_config=StrategyConfig(
            strategy_id=ScriptedStrategy.strategy_id,
            enabled=True,
            instruments=["USDJPY"],
        ),
    )


def run_slice(
    costs: CostModel,
    *,
    seed: int = 7,
    count: int = 2000,
    plan: dict[int, PositionDirection] | None = None,
    stop_distance_pips: Decimal = Decimal(200),
    risk_config: RiskConfig | None = None,
) -> BacktestResult:
    ticks = synthetic_ticks(
        spec=usdjpy_spec(), start=DATASET_START, count=count, seed=seed
    )
    engine = build_engine(
        costs,
        seed=seed,
        plan=plan,
        stop_distance_pips=stop_distance_pips,
        risk_config=risk_config,
    )
    return engine.run(ticks)


def test_same_dataset_config_seed_reproduces_identical_runs():
    results = [run_slice(STRESS_SCENARIOS["normal"]) for _ in range(10)]
    base = results[0]
    assert int(base.metrics["fills"]) >= 3  # OPEN, CLOSE, reversal OPEN
    for other in results[1:]:
        assert other.fills == base.fills
        assert other.metrics == base.metrics
        assert other.equity_curve == base.equity_curve
        assert other.risk_rejections == base.risk_rejections


def test_rerunning_the_same_engine_instance_is_deterministic():
    # Strategies hold per-run state (tick counters, setup memos); the engine
    # builds them from a factory so a reused engine cannot leak state.
    ticks = synthetic_ticks(spec=usdjpy_spec(), start=DATASET_START, count=2000, seed=7)
    engine = build_engine(STRESS_SCENARIOS["normal"])
    first = engine.run(ticks)
    second = engine.run(ticks)
    assert second.fills == first.fills
    assert second.metrics == first.metrics


def test_fills_never_apply_ahead_of_the_replay_clock():
    # With 150ms latency and 1s ticks the OPEN signalled at ordinal 300
    # fills on tick 301 — and equity must not move before that tick: a fill
    # applied at signal time would leak the future into snapshots and sizing.
    result = run_slice(STRESS_SCENARIOS["normal"])
    initial = Decimal(result.metrics["initial_equity"])
    assert result.fills[0].at == DATASET_START + timedelta(seconds=301)
    assert all(equity == initial for _, equity in result.equity_curve[:301])
    assert result.equity_curve[301][1] != initial


def test_dataset_hash_is_stable_and_seed_sensitive():
    spec = usdjpy_spec()
    a = synthetic_ticks(spec=spec, start=DATASET_START, count=500, seed=7)
    b = synthetic_ticks(spec=spec, start=DATASET_START, count=500, seed=7)
    c = synthetic_ticks(spec=spec, start=DATASET_START, count=500, seed=8)
    assert dataset_hash(a) == dataset_hash(b)
    assert dataset_hash(a) != dataset_hash(c)


def test_cost_stress_strictly_degrades_net_pnl():
    # A backtest whose PnL does not respond to worse costs has a simulator
    # that is not actually applied. Wide stops keep the trade path identical
    # across scenarios so the comparison isolates execution costs.
    normal = run_slice(STRESS_SCENARIOS["normal"])
    stressed = run_slice(STRESS_SCENARIOS["spread_x2"])
    assert stressed.fills != normal.fills  # prices differ, schedule matches
    assert [f.at for f in stressed.fills] == [f.at for f in normal.fills]
    assert Decimal(stressed.metrics["execution_cost"]) > Decimal(
        normal.metrics["execution_cost"]
    )
    assert Decimal(stressed.metrics["net_pnl"]) < Decimal(normal.metrics["net_pnl"])


def test_full_lifecycle_flows_through_the_pipeline():
    result = run_slice(STRESS_SCENARIOS["normal"])
    actions = [(f.action, f.side, f.direction) for f in result.fills]
    assert actions[0] == ("OPEN", "BUY", "LONG")
    assert ("CLOSE", "SELL", "LONG") in actions  # ticket-referenced exit
    assert ("OPEN", "SELL", "SHORT") in actions  # reversal opens fresh
    assert result.risk_rejections == []
    assert result.metrics["open_positions_at_end"] == "1"
    # Accounting closes: final equity = initial + realized + unrealized.
    assert Decimal(result.metrics["final_equity"]) == (
        Decimal(result.metrics["initial_equity"])
        + Decimal(result.metrics["realized_pnl"])
        + Decimal(result.metrics["unrealized_pnl"])
    )


def test_partial_exit_keeps_remainder_tracked_and_withholds_reversal():
    # With partial fills forced on, the flip's CLOSE only half-fills. The
    # remainder must stay attributed — its later protection fill settles
    # against the original entry — and the reversal OPEN stays withheld
    # because the targeted ticket never went flat.
    costs = CostModel(
        latency_ms=0.0, slippage_sigma_pips=0.0, partial_fill_probability=1.0
    )
    result = run_slice(
        costs,
        plan={300: PositionDirection.LONG, 400: PositionDirection.SHORT},
        stop_distance_pips=Decimal(10),
    )
    closes = [f for f in result.fills if f.action == "CLOSE"]
    assert closes and closes[0].quantity == Decimal(2000)  # half of the 5000 held
    protection = [f for f in result.fills if f.origin == "PROTECTION"]
    assert protection and protection[0].quantity == Decimal(3000)  # the remainder
    # The reversal never opens: no SHORT fill, LONG and SHORT never coexist.
    assert not any(f.direction == "SHORT" and f.action == "OPEN" for f in result.fills)
    assert result.metrics["open_positions_at_end"] == "0"
    assert Decimal(result.metrics["final_equity"]) == (
        Decimal(result.metrics["initial_equity"])
        + Decimal(result.metrics["realized_pnl"])
        + Decimal(result.metrics["unrealized_pnl"])
    )


def test_pending_entries_reserve_the_position_cap():
    # Latency longer than the tick interval: the first OPEN is approved but
    # unfilled when the second signal arrives, and its queued command must
    # already occupy the position cap.
    costs = CostModel(latency_ms=2500.0, slippage_sigma_pips=0.0)
    result = run_slice(
        costs, plan={300: PositionDirection.LONG, 301: PositionDirection.LONG}
    )
    opens = [f for f in result.fills if f.action == "OPEN"]
    assert len(opens) == 1
    assert any("MAX_OPEN_POSITIONS" in codes for _, codes in result.risk_rejections)


def test_flip_closes_every_held_ticket():
    # With the cap raised, a second same-direction signal INCREASEs via a
    # second hedging ticket; the flip must close BOTH tickets before the
    # reversal opens, not just the latest one.
    config = slice_risk_config().model_copy(update={"max_open_positions": 2})
    result = run_slice(
        STRESS_SCENARIOS["normal"],
        plan={
            300: PositionDirection.LONG,
            500: PositionDirection.LONG,
            1200: PositionDirection.SHORT,
        },
        risk_config=config,
    )
    closes = [f for f in result.fills if f.action == "CLOSE"]
    assert len(closes) == 2
    assert all(f.direction == "LONG" and f.side == "SELL" for f in closes)
    reversal = [f for f in result.fills if f.action == "OPEN" and f.direction == "SHORT"]
    assert reversal, "reversal OPEN must execute once every leg is flat"
    assert result.risk_rejections == []
    assert result.metrics["open_positions_at_end"] == "1"


def test_protection_fill_closes_position_and_is_tracked():
    # A tight stop on a random walk fires broker-side protection; the fill
    # is a first-class PROTECTION fill, never an untracked one.
    result = run_slice(
        STRESS_SCENARIOS["normal"],
        plan={100: PositionDirection.LONG},
        stop_distance_pips=Decimal(3),
    )
    protection = [f for f in result.fills if f.origin == "PROTECTION"]
    assert protection, "expected the tight SL to fire within the dataset"
    assert protection[0].action == "PROTECTION_CLOSE"
    assert result.metrics["open_positions_at_end"] == "0"
