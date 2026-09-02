"""Vertical-slice acceptance tests.

The first acceptance criterion of the backtest system is NOT profitability:

1. Same dataset + config + seed -> identical signals, fills, PnL, metrics.
2. Worse execution costs (spread/slippage stress) -> strictly worse net PnL.
3. The full order lifecycle (OPEN -> ticket-referenced CLOSE -> reversal
   OPEN, protection fills) flows through Risk -> OMS -> Simulator -> Ledger.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.support import make_tick, usdjpy_spec
from trading.backtest.costs import STRESS_SCENARIOS, CostModel
from trading.backtest.data import dataset_hash, synthetic_ticks
from trading.backtest.engine import BacktestEngine, BacktestResult, ScriptedStrategy
from trading.domain.position import PositionDirection
from trading.domain.risk import EventRiskMode
from trading.risk.engine import RiskConfig
from trading.risk.event_risk import EventRiskCalendar, EventRiskWindow
from trading.strategy.base import StrategyConfig

DATASET_START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def slice_risk_config() -> RiskConfig:
    # Halt thresholds are opened up: these tests assert engine mechanics,
    # not the production loss limits (covered by risk-engine tests).
    return RiskConfig(
        trading_enabled=True,
        max_units_per_symbol={"USDJPY": 10000},
        absolute_max_spread_pips={"USDJPY": Decimal(10)},
        max_risk_per_trade_pct=Decimal("1.0"),
        portfolio_stop_risk_budget_pct=Decimal(10),
        max_currency_net_exposure_pct=Decimal(1000),
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
    event_risk: EventRiskCalendar | None = None,
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
        event_risk=event_risk,
    )


def run_slice(
    costs: CostModel,
    *,
    seed: int = 7,
    count: int = 2000,
    plan: dict[int, PositionDirection] | None = None,
    stop_distance_pips: Decimal = Decimal(200),
    risk_config: RiskConfig | None = None,
    event_risk: EventRiskCalendar | None = None,
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
        event_risk=event_risk,
    )
    return engine.run(ticks)


def test_a_central_bank_window_blocks_entries_in_replay():
    # Replay grades against the same calendar live does. Ignoring it here
    # would report trades the live path refuses, which is the one difference
    # between the two that research cannot see.
    window = EventRiskWindow(
        name="dual_central_bank_cluster",
        first_event_at=DATASET_START,
        last_event_at=DATASET_START,
        pre_hours=48,
        post_hours=24,
        actions={ScriptedStrategy.horizon: EventRiskMode.HALT},
    )

    halted = run_slice(
        STRESS_SCENARIOS["normal"],
        event_risk=EventRiskCalendar(
            [window], (DATASET_START - timedelta(days=1), DATASET_START + timedelta(days=60))
        ),
    )

    assert halted.fills == []
    assert any("EVENT_MODE_ALLOWS_ENTRY" in codes for _, codes in halted.risk_rejections)


def test_a_replay_beyond_the_recorded_calendar_falls_back_to_the_default():
    # The dataset predates every recorded meeting, so the calendar has nothing
    # to say about it and the configured default applies — the same as running
    # with no calendar at all. NORMAL here, so the run is unchanged.
    far_off = EventRiskWindow(
        name="dual_central_bank_cluster",
        first_event_at=DATASET_START + timedelta(days=30),
        last_event_at=DATASET_START + timedelta(days=30),
        pre_hours=48,
        post_hours=24,
        actions={ScriptedStrategy.horizon: EventRiskMode.HALT},
    )

    with_calendar = run_slice(
        STRESS_SCENARIOS["normal"],
        # Recorded only around that distant decision; the dataset predates it.
        event_risk=EventRiskCalendar(
            [far_off],
            (DATASET_START + timedelta(days=28), DATASET_START + timedelta(days=32)),
        ),
    )
    without = run_slice(STRESS_SCENARIOS["normal"])

    assert with_calendar.fills == without.fills


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
    # The curve is decimated to minute points plus fill instants, so the
    # first moved point IS the fill instant, and everything before it holds
    # the initial equity.
    result = run_slice(STRESS_SCENARIOS["normal"])
    initial = Decimal(result.metrics["initial_equity"])
    fill_at = DATASET_START + timedelta(seconds=301)
    assert result.fills[0].at == fill_at
    assert all(equity == initial for at, equity in result.equity_curve if at < fill_at)
    moved = [at for at, equity in result.equity_curve if equity != initial]
    assert moved and moved[0] == fill_at


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


def test_opposite_signal_during_entry_latency_becomes_a_flip():
    # A SHORT signal while the LONG entry is still in flight must resolve as
    # CLOSE -> reversal OPEN after the entry fills — never as a parallel
    # opposite OPEN that leaves LONG and SHORT on the book together.
    costs = CostModel(latency_ms=2500.0, slippage_sigma_pips=0.0)
    config = slice_risk_config().model_copy(update={"max_open_positions_per_symbol": 2, "max_open_positions_portfolio": 2})
    result = run_slice(
        costs,
        plan={300: PositionDirection.LONG, 301: PositionDirection.SHORT},
        risk_config=config,
    )
    actions = [(f.action, f.direction) for f in result.fills]
    assert ("OPEN", "LONG") in actions
    assert ("CLOSE", "LONG") in actions
    assert ("OPEN", "SHORT") in actions
    assert result.metrics["open_positions_at_end"] == "1"


def test_repeated_opposite_signal_during_exit_latency_does_not_duplicate_flip():
    # A second SHORT signal while the flip's CLOSE is still in flight must
    # wait for the exit sequence: exactly one reversal OPEN, the repeat
    # resolves as an INCREASE afterwards — never a second CLOSE + barrier
    # stacking two same-direction OPENs.
    costs = CostModel(latency_ms=2500.0, slippage_sigma_pips=0.0)
    config = slice_risk_config().model_copy(update={"max_open_positions_per_symbol": 2, "max_open_positions_portfolio": 2})
    result = run_slice(
        costs,
        plan={
            300: PositionDirection.LONG,
            301: PositionDirection.SHORT,
            302: PositionDirection.SHORT,
        },
        risk_config=config,
    )
    closes = [f for f in result.fills if f.action == "CLOSE"]
    short_opens = [
        f for f in result.fills if f.action == "OPEN" and f.direction == "SHORT"
    ]
    increases = [f for f in result.fills if f.action == "INCREASE"]
    assert len(closes) == 1
    assert len(short_opens) == 1
    assert len(increases) == 1
    assert result.rejected_commands == 0


def test_late_old_tick_does_not_rewind_valuation():
    # The last DELIVERED tick can carry an older broker time (reception
    # order); final equity must stay marked at the newest broker-time price
    # already known, exactly like InMemoryMarketData.latest_tick().
    spec = usdjpy_spec()
    base = synthetic_ticks(spec=spec, start=DATASET_START, count=500, seed=7)
    late_old = make_tick(
        "158.500",
        "158.504",
        time=DATASET_START + timedelta(seconds=100),
        received_at=DATASET_START + timedelta(seconds=600),
    )
    plan = {300: PositionDirection.LONG}
    without = build_engine(STRESS_SCENARIOS["normal"], plan=plan).run(base)
    with_late = build_engine(STRESS_SCENARIOS["normal"], plan=plan).run(
        [*base, late_old]
    )
    assert with_late.metrics["final_equity"] == without.metrics["final_equity"]
    assert with_late.metrics["unrealized_pnl"] == without.metrics["unrealized_pnl"]


def test_hourly_snapshots_restore_loss_window_baselines():
    # Risk loss windows (JST daily / rolling 24h) need baselines between
    # fills: a position held across a time boundary must leave periodic
    # snapshots, not only fill-time ones.
    ticks = synthetic_ticks(
        spec=usdjpy_spec(),
        start=DATASET_START,
        count=180,
        seed=7,
        interval_seconds=60,
    )
    engine = build_engine(
        STRESS_SCENARIOS["normal"], plan={10: PositionDirection.LONG}
    )
    result = engine.run(ticks)
    hours = {
        s.observed_at.replace(minute=0, second=0, microsecond=0)
        for s in result.snapshots
    }
    assert DATASET_START + timedelta(hours=1) in hours
    assert DATASET_START + timedelta(hours=2) in hours


def test_open_positions_marked_at_stressed_spread():
    # Fills and protection use the STRESSED bid/ask, so an open position at
    # the end must be marked with the same stressed executable price —
    # otherwise final equity omits the widened close-side spread.
    costs = CostModel(spread_multiplier=2.0, latency_ms=0.0, slippage_sigma_pips=0.0)
    ticks = synthetic_ticks(spec=usdjpy_spec(), start=DATASET_START, count=2000, seed=7)
    result = build_engine(costs, plan={300: PositionDirection.LONG}).run(ticks)

    entry_tick, last_tick = ticks[300], ticks[-1]
    stressed_entry = entry_tick.mid + entry_tick.spread  # x2 spread ask
    stressed_mark = last_tick.mid - last_tick.spread  # x2 spread bid
    expected_unrealized = (stressed_mark - stressed_entry) * Decimal(5000)
    assert Decimal(result.metrics["unrealized_pnl"]) == expected_unrealized


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
    assert any("MAX_OPEN_POSITIONS_PER_SYMBOL" in codes for _, codes in result.risk_rejections)


def test_flip_closes_every_held_ticket():
    # With the cap raised, a second same-direction signal INCREASEs via a
    # second hedging ticket; the flip must close BOTH tickets before the
    # reversal opens, not just the latest one.
    config = slice_risk_config().model_copy(update={"max_open_positions_per_symbol": 2, "max_open_positions_portfolio": 2})
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


def test_multi_ticket_exit_batch_does_not_release_deferred_early():
    # Two tickets means the flip queues two EXIT commands that become
    # executable on the SAME tick. A signal held while they are in flight
    # must stay held until the LAST leg resolves: released after the first
    # one, it would target the still-open second ticket with a duplicate
    # CLOSE and stack a second reversal OPEN on top of the real one.
    costs = CostModel(latency_ms=2500.0, slippage_sigma_pips=0.0)
    config = slice_risk_config().model_copy(update={"max_open_positions_per_symbol": 2, "max_open_positions_portfolio": 2})
    result = run_slice(
        costs,
        plan={
            300: PositionDirection.LONG,
            500: PositionDirection.LONG,
            700: PositionDirection.SHORT,
            701: PositionDirection.SHORT,
        },
        risk_config=config,
    )
    closes = [f for f in result.fills if f.action == "CLOSE"]
    short_opens = [
        f for f in result.fills if f.action == "OPEN" and f.direction == "SHORT"
    ]
    short_increases = [
        f for f in result.fills if f.action == "INCREASE" and f.direction == "SHORT"
    ]
    # Both legs of the flip exit on the same tick: the batch the fix is about.
    assert len(closes) == 2
    assert len({f.at for f in closes}) == 1
    assert len(short_opens) == 1
    # The held signal resolves against the post-reversal book, as an INCREASE.
    assert len(short_increases) == 1
    assert result.rejected_commands == 0
    assert result.risk_rejections == []


def test_zero_latency_fill_is_protected_on_the_same_tick():
    # A broker watches SL/TP from the instant a position exists. With no
    # latency the entry fills on the signalling tick, and the stop sits
    # inside the stressed spread, so protection belongs on THAT tick — not
    # on the next one (nowhere at all, had it been the last tick).
    # The stop is derived from the UNSTRESSED ask of the signalling tick
    # while protection compares against the STRESSED bid, so the crossing
    # condition on this 0.6-pip-spread dataset is
    #   stop_distance_pips <= 0.3 + 0.3 * spread_multiplier
    # i.e. 3 <= 3.3 here. No randomness enters that comparison.
    costs = CostModel(spread_multiplier=10.0, latency_ms=0.0, slippage_sigma_pips=0.0)
    result = run_slice(
        costs,
        count=400,
        plan={300: PositionDirection.LONG},
        stop_distance_pips=Decimal(3),
    )
    opens = [f for f in result.fills if f.action == "OPEN"]
    protection = [f for f in result.fills if f.origin == "PROTECTION"]
    assert len(opens) == 1
    assert len(protection) == 1
    assert protection[0].at == opens[0].at
    assert result.metrics["open_positions_at_end"] == "0"


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


def test_run_stream_reproduces_run_exactly():
    # The streaming path must be the same replay, not a sibling: identical
    # fills, metrics and curve for the same dataset, config and seed.
    ticks = synthetic_ticks(spec=usdjpy_spec(), start=DATASET_START, count=2000, seed=7)
    materialized = build_engine(STRESS_SCENARIOS["normal"]).run(ticks)
    streamed = build_engine(STRESS_SCENARIOS["normal"]).run_stream(iter(ticks))

    assert streamed.fills == materialized.fills
    assert streamed.metrics == materialized.metrics
    assert streamed.equity_curve == materialized.equity_curve
    assert streamed.risk_rejections == materialized.risk_rejections


def test_equity_curve_holds_minutes_and_fill_instants_not_every_tick():
    # 2000 one-second ticks span ~33 minutes; a per-tick curve would hold
    # 2000 points and a months-long replay millions. Every fill instant
    # still appears, so the curve never hides where equity actually moved.
    result = run_slice(STRESS_SCENARIOS["normal"])

    assert len(result.equity_curve) < 100
    curve_times = {at for at, _ in result.equity_curve}
    assert {fill.at for fill in result.fills} <= curve_times


def test_curve_ends_on_the_closing_equity_with_duplicate_final_instants():
    # Several final ticks can share one known time (the stored series keys on
    # bid/ask too). A fill on the first of them appends a curve point at that
    # instant; the later same-instant tick still moves equity, and the curve
    # must end on the truly final value, not the fill-time one.
    ticks = synthetic_ticks(spec=usdjpy_spec(), start=DATASET_START, count=600, seed=7)
    shifted = ticks[-1].model_copy(
        update={
            "bid": ticks[-1].bid + Decimal("0.100"),
            "ask": ticks[-1].ask + Decimal("0.100"),
        }
    )
    result = build_engine(
        STRESS_SCENARIOS["normal"], plan={598: PositionDirection.LONG}
    ).run([*ticks, shifted])

    assert result.fills and result.fills[-1].at == ticks[-1].known_time
    assert result.equity_curve[-1][1] == Decimal(result.metrics["final_equity"])
