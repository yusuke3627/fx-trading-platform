"""Vertical-slice backtest engine.

Wires the full order lifecycle over recorded/synthetic ticks:

    Tick -> ReplayClock -> Strategy -> PositionIntent -> RiskDecision
         -> ExecutionCommand -> ExecutionSimulator -> Fill
         -> VirtualPositionLedger -> AccountSnapshot -> metrics

The goal of this slice is reproducing ONE order exactly, not strategy alpha:
the acceptance criteria are determinism (same dataset + config + seed ->
identical fills, PnL and metrics) and cost sensitivity (worse spread/slippage
must worsen net PnL).

Commands never fill ahead of the clock: an approved command waits in a
pending queue until the replay reaches a tick at or past its latency window,
and only that tick's fill mutates the book, the ledger and equity. Follow-up
intents of a flip (the reversal OPEN) are risk-evaluated only after every
close leg resolved — the OMS re-delta convention, collapsed to the slice.

Simplifications vs live, by design: no outbox/worker processes, no swap
accrual, and open positions at the end of the replay are marked to market
instead of being force-closed.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import chain
from uuid import uuid4

from trading.backtest.clock import Clock, ReplayClock
from trading.backtest.costs import CostModel
from trading.backtest.market import TICK_HORIZON_SECONDS, ReplayMarketData
from trading.backtest.simulator import ExecutionSimulator
from trading.data.features import ReplayFeatureTimeline
from trading.data.market.bars import BarBuilder
from trading.domain.account import AccountMode, AccountSnapshot
from trading.domain.event import EventEnvelope
from trading.domain.exposure import OpenPositionExposure
from trading.domain.instrument import InstrumentSpec
from trading.domain.intent import PositionIntent
from trading.domain.market import Tick
from trading.domain.order import ExecutionCommand, ExecutionSide
from trading.domain.position import BrokerPosition, PositionAction, PositionDirection
from trading.domain.risk import EventRiskMode, KillSwitchLevel
from trading.domain.signal import StrategySignal
from trading.indicators import IndicatorService
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.regime import RuleBasedRegimeService
from trading.oms.service import OMSService
from trading.portfolio.exposure import CurrencyExposureService
from trading.portfolio.manager import PortfolioManager, SizingInput
from trading.portfolio.virtual_ledger import VirtualPositionLedger
from trading.risk.conversion import MarketQuoteConversionService
from trading.risk.engine import PreTradeContext, RiskConfig, RiskEngine
from trading.risk.event_risk import EventRiskCalendar
from trading.strategy.base import (
    Strategy,
    StrategyConfig,
    StrategyContext,
    StrategyHorizon,
)

ENGINE_VERSION = "0.4.0"


class ScriptedStrategy(Strategy):
    """Deterministic probe strategy for the vertical slice.

    Emits a direction signal at fixed tick ordinals; it proves the pipeline,
    never an edge. Exits happen through the flip path (opposite signal ->
    CLOSE + OPEN) or broker-side protection.
    """

    strategy_id = "vertical_slice_probe"
    strategy_version = "0.1.0"
    horizon = StrategyHorizon.INTRADAY

    def __init__(
        self,
        plan: dict[int, PositionDirection],
        stop_distance_pips: Decimal = Decimal(10),
    ) -> None:
        self._plan = dict(plan)
        self._stop = stop_distance_pips
        self._seen = 0

    async def on_event(
        self, event: EventEnvelope, context: StrategyContext
    ) -> list[StrategySignal]:
        ordinal = self._seen
        self._seen += 1
        direction = self._plan.get(ordinal)
        if direction is None:
            return []
        symbol = context.config.instruments[0]
        if not self._new_setup(symbol, direction, ordinal):
            return []
        return [
            self.make_signal(
                context,
                symbol=symbol,
                direction=direction,
                conviction=1.0,
                stop_distance_pips=self._stop,
                expected_horizon_seconds=3600,
                reason_codes=["SCRIPTED"],
            )
        ]


class SimulatedBroker:
    """BrokerPositionReader over the simulator book (fresh-select semantics)."""

    def __init__(self, simulator: ExecutionSimulator, clock: Clock) -> None:
        self._simulator = simulator
        self._clock = clock

    def position(self, ticket: str) -> BrokerPosition | None:
        held = self._simulator.position(ticket)
        if held is None:
            return None
        return BrokerPosition(
            broker_position_ticket=held.position_id,
            broker_position_identifier=held.position_id,
            symbol=held.symbol,
            direction=held.direction,
            quantity=held.quantity,
            entry_price=held.entry_price,
            stop_loss=held.stop_loss,
            take_profit=held.take_profit,
            observed_at=self._clock.now(),
        )

    def net_exposure(self, symbol: str) -> Decimal:
        total = Decimal(0)
        for p in self._simulator.open_positions(symbol):
            total += p.quantity if p.direction is PositionDirection.LONG else -p.quantity
        return total

    def gross_exposure(self, symbol: str) -> Decimal:
        return sum(
            (p.quantity for p in self._simulator.open_positions(symbol)), Decimal(0)
        )


@dataclass(frozen=True)
class FillRecord:
    """Deterministic view of one fill (broker-generated ids excluded, so two
    runs of the same dataset + seed compare equal record-by-record)."""

    at: datetime
    strategy_id: str
    action: str
    side: str
    direction: str
    quantity: Decimal
    price: Decimal
    mid: Decimal
    origin: str


@dataclass
class BacktestResult:
    symbol: str
    fills: list[FillRecord]
    equity_curve: list[tuple[datetime, Decimal]]
    snapshots: list[AccountSnapshot]
    risk_rejections: list[tuple[datetime, tuple[str, ...]]]
    rejected_commands: int
    metrics: dict[str, str]


@dataclass
class _Barrier:
    """Releases the follow-up intents of a flip only when every targeted
    ticket is confirmed flat by a fresh select — a partially filled or
    rejected close leg withholds the reversal instead of stacking LONG and
    SHORT."""

    tickets: list[str]
    outstanding: int
    follow_up: list[PositionIntent]
    signal: StrategySignal


@dataclass
class _Pending:
    """A risk-approved command waiting for the clock to reach its fill."""

    command: ExecutionCommand
    strategy_id: str
    action: str
    kind: str  # "ENTRY" | "EXIT"
    barrier: _Barrier | None = None


@dataclass
class _RunState:
    initial_equity: Decimal
    realized: Decimal = Decimal(0)
    high_water_mark: Decimal = Decimal(0)
    snapshots: list[AccountSnapshot] = field(default_factory=list)
    fills: list[FillRecord] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    risk_rejections: list[tuple[datetime, tuple[str, ...]]] = field(default_factory=list)
    rejected_commands: int = 0
    max_drawdown: Decimal = Decimal(0)
    last_curve_minute: datetime | None = None
    pending: list[_Pending] = field(default_factory=list)
    # ticket -> owning strategy / entry marks for PnL attribution; a strategy
    # can hold several tickets (INCREASE opens a new one on hedging).
    ticket_owner: dict[str, str] = field(default_factory=dict)
    entry_price: dict[str, Decimal] = field(default_factory=dict)
    entry_mid: dict[str, Decimal] = field(default_factory=dict)
    open_tickets: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    gross_mid_closed: Decimal = Decimal(0)
    # Latest KNOWN price on the broker timeline: valuation must never rewind
    # to a late-received tick with an older broker time.
    marking_tick: Tick | None = None
    last_snapshot_hour: datetime | None = None
    # Signals held back while an entry for their slot is still in flight.
    deferred: dict[tuple[str, str], list[StrategySignal]] = field(default_factory=dict)


def signed_pnl(
    direction: PositionDirection, entry: Decimal, exit_price: Decimal, quantity: Decimal
) -> Decimal:
    """Quote-currency PnL of a closed quantity."""
    if direction is PositionDirection.LONG:
        return (exit_price - entry) * quantity
    return (entry - exit_price) * quantity


@dataclass
class _Wiring:
    """Per-run components; built fresh for every run() call."""

    clock: ReplayClock
    market: ReplayMarketData
    bar_builders: list[BarBuilder]
    simulator: ExecutionSimulator
    broker: SimulatedBroker
    oms: OMSService
    ledger: VirtualPositionLedger
    portfolio: PortfolioManager
    risk: RiskEngine
    exposure: CurrencyExposureService
    strategy: Strategy
    context: StrategyContext


class BacktestEngine:
    def __init__(
        self,
        *,
        risk_config: RiskConfig,
        spec: InstrumentSpec,
        costs: CostModel,
        seed: int,
        strategy_factory: Callable[[], Strategy],
        strategy_config: StrategyConfig,
        initial_equity: Decimal = Decimal(1_000_000),
        account_mode: AccountMode = AccountMode.HEDGING,
        event_risk: EventRiskCalendar | None = None,
        features: ReplayFeatureTimeline | None = None,
        evaluate_from: datetime | None = None,
    ) -> None:
        if account_mode is not AccountMode.HEDGING:
            raise ValueError("the vertical slice runs on HEDGING only")
        self._risk_config = risk_config
        self._spec = spec
        self._costs = costs
        self._seed = seed
        # A factory, not an instance: strategies hold per-run state (setup
        # memos, tick counters), so sharing one across runs breaks determinism.
        self._strategy_factory = strategy_factory
        self._strategy_config = strategy_config
        self._initial_equity = initial_equity
        self._mode = account_mode
        # Optional: a research run over a period whose meeting calendar is not
        # loaded grades on the configured default rather than pretending every
        # instant was quiet.
        self._event_risk = event_risk
        # Optional for the same reason: the synthetic vertical slice has no
        # PIT rows to step through, and an empty store is the honest state
        # for it. A research run wires the same timeline live shadow polls.
        self._features = features
        # Warm-up boundary: before this instant bars, indicators and features
        # build state but the strategy is not asked, so a research period's
        # first evaluation already sees its slowest window populated.
        self._evaluate_from = evaluate_from

    def run(self, ticks: list[Tick]) -> BacktestResult:
        ordered = sorted(ticks, key=lambda t: t.known_time)
        if not ordered:
            raise ValueError("backtest requires at least one tick")
        return self._replay(iter(ordered))

    def run_stream(self, ticks: Iterable[Tick]) -> BacktestResult:
        """Replay from an iterator without materializing the dataset.

        The caller supplies ticks in known-time order — a stored series read
        in (event_time, id) order is, under the ADR-007 reconstruction. An
        out-of-order tick fails the replay clock's regression guard instead
        of being silently resorted: resorting needs the whole dataset in
        memory, which is exactly what this path exists to avoid.
        """
        return self._replay(iter(ticks))

    def _replay(self, ticks: Iterator[Tick]) -> BacktestResult:
        try:
            first = next(ticks)
        except StopIteration:
            raise ValueError("backtest requires at least one tick") from None

        w = self._wire(first.known_time - timedelta(seconds=1))
        state = _RunState(initial_equity=self._initial_equity)
        state.high_water_mark = self._initial_equity
        # The flat opening baseline carries the period's opening timestamp,
        # not the warm-up's, so the output series never reaches before the
        # measured run.
        baseline_at = (
            self._evaluate_from if self._evaluate_from is not None else w.clock.now()
        )
        state.snapshots.append(self._snapshot(state, w.simulator, baseline_at))

        def handle(item: Tick) -> None:
            fills_before = len(state.fills)
            # Features first: everything after this point may read the store,
            # and what it reads has to be the snapshot at this clock instant.
            if self._features is not None:
                self._features.advance(w.clock.now())
            w.market.add_tick(item)
            # Bars close before the strategy is evaluated, so the candle that
            # this tick completed is readable on the very tick that closed it.
            for builder in w.bar_builders:
                bar = builder.on_tick(item)
                if bar is not None:
                    w.market.add_bar(bar)
            if state.marking_tick is None or item.time >= state.marking_tick.time:
                state.marking_tick = item
            # Warm-up ticks build bars, indicators and features but leave no
            # trace in the outputs: the strategy is not asked, and the equity
            # curve, snapshots and metrics start at the period's opening
            # instant — a lead-in must not stretch the measured run.
            evaluating = (
                self._evaluate_from is None or w.clock.now() >= self._evaluate_from
            )
            # Risk loss windows (JST daily / rolling 24h) need baselines
            # between fills: a position held across a boundary must leave
            # periodic snapshots, not only fill-time ones.
            hour = w.clock.now().replace(minute=0, second=0, microsecond=0)
            if evaluating:
                if state.last_snapshot_hour is None:
                    state.last_snapshot_hour = hour
                elif hour > state.last_snapshot_hour:
                    state.last_snapshot_hour = hour
                    state.snapshots.append(
                        self._snapshot(state, w.simulator, w.clock.now())
                    )

            # Broker events precede strategy evaluation: pending command
            # fills first, then broker-side protection on the same price.
            self._apply_pending(state, w, item)
            self._apply_protection(state, w, item)

            if evaluating:
                envelope = EventEnvelope(
                    event_id=uuid4(),
                    event_type="market.tick",
                    source="replay",
                    retrieved_at=item.known_time,
                    known_at=item.known_time,
                )
                signals = runner_loop.run(w.strategy.on_event(envelope, w.context))
                for signal in signals:
                    self._process_signal(state, w, signal, tick=item)
            # Zero-latency commands created this tick fill on this tick;
            # anything slower stays queued for a later tick. A position born
            # here is under broker protection from the instant it exists, so
            # it is swept on THIS tick too — not only from the next one.
            self._apply_pending(state, w, item)
            self._apply_protection(state, w, item)

            if evaluating:
                equity = self._equity(state, w.simulator)
                state.high_water_mark = max(state.high_water_mark, equity)
                # Drawdown is tracked on every evaluated tick; the curve is a
                # display series, so per-minute points (plus every fill
                # instant) lose no metric fidelity while a months-long replay
                # stays bounded in memory.
                state.max_drawdown = max(
                    state.max_drawdown, state.high_water_mark - equity
                )
                minute = w.clock.now().replace(second=0, microsecond=0)
                if (
                    minute != state.last_curve_minute
                    or len(state.fills) != fills_before
                ):
                    state.last_curve_minute = minute
                    state.equity_curve.append((w.clock.now(), equity))

        with asyncio.Runner() as runner_loop:
            for item in chain([first], ticks):
                w.clock.advance_to(item.known_time)
                handle(item)

        # The decimated curve still ends on the replay's closing equity: a
        # reader of the series must see where the run actually finished.
        if state.equity_curve and state.equity_curve[-1][0] != w.clock.now():
            state.equity_curve.append(
                (w.clock.now(), self._equity(state, w.simulator))
            )

        return self._result(state, w.simulator)

    def _apply_protection(self, state: _RunState, w: _Wiring, tick: Tick) -> None:
        """Broker-side SL/TP sweep of the whole book at this tick's price.

        Re-running it after a later batch of fills is safe and required: the
        check consumes no randomness and is idempotent for a given
        (position, tick), so a position that survived an earlier sweep cannot
        fire on a second one at the same price — only positions that did not
        yet exist can."""
        for position in w.simulator.open_positions(self._spec.symbol):
            fill = w.simulator.check_protection(position, tick)
            if fill is None:
                continue
            self._settle_close(
                state,
                w,
                ticket=position.position_id,
                direction=position.direction,
                quantity=fill.quantity,
                price=fill.price,
                at=fill.broker_time,
                mid=tick.mid,
                action="PROTECTION_CLOSE",
                side=fill.side,
                origin=fill.origin.value,
            )
            state.snapshots.append(self._snapshot(state, w.simulator, w.clock.now()))

    def _wire(self, start: datetime) -> _Wiring:
        clock = ReplayClock(start)
        strategy = self._strategy_factory()
        # Retention must hold the widest tick window this configuration lets
        # the strategy request, or a legitimately re-tuned window would be
        # refused mid-replay.
        market = ReplayMarketData(
            tick_horizon_seconds=max(
                TICK_HORIZON_SECONDS,
                type(strategy).tick_window_seconds(self._strategy_config),
            )
        )
        market.set_instrument(self._spec)
        # Replay-visible quotes only: the conversion service reads the same
        # market, so sizing can never use a rate that was not yet known.
        # 単一銘柄 timeline の現行 engine は換算用の別銘柄 quote（例:
        # EURUSD backtest に必要な USDJPY）を運べない。その場合 sizing は
        # CONVERSION_RATE_UNAVAILABLE で fail-close する（意図した挙動）。
        # 換算 quote を含む複数銘柄 timeline は 4-pair PIT replay（#66）で。
        conversion = MarketQuoteConversionService(
            market,
            [self._spec],
            max_quote_age_seconds=self._risk_config.quote_max_age_seconds,
        )
        simulator = ExecutionSimulator(self._costs, self._spec, self._seed, self._mode)
        broker = SimulatedBroker(simulator, clock)
        ledger = VirtualPositionLedger(clock)
        # reset() every run: the timeline is stateful and this wiring promises
        # per-run freshness — a second run() must not start where the first
        # one's pointer stopped.
        if self._features is not None:
            self._features.reset(start)
            features = self._features.store
        else:
            features = InMemoryFeatureStore()
        return _Wiring(
            clock=clock,
            market=market,
            # Configuration owns timeframe selection: a strategy that declares
            # no timeframes gets no bars, and none are built for it.
            bar_builders=[
                BarBuilder(self._spec.symbol, timeframe)
                for timeframe in self._strategy_config.timeframes.all()
            ],
            simulator=simulator,
            broker=broker,
            oms=OMSService(account_mode=self._mode, broker=broker, clock=clock),
            ledger=ledger,
            portfolio=PortfolioManager(ledger, clock, conversion),
            risk=RiskEngine(self._risk_config, clock, conversion),
            exposure=CurrencyExposureService(conversion),
            strategy=strategy,
            context=StrategyContext(
                clock=clock,
                market=market,
                indicators=IndicatorService(market),
                features=features,
                regime=RuleBasedRegimeService(features),
                portfolio=ledger,
                config=self._strategy_config,
            ),
        )

    def _process_signal(
        self, state: _RunState, w: _Wiring, signal: StrategySignal, *, tick: Tick
    ) -> None:
        slot = (signal.strategy_id, signal.symbol)
        if any(
            p.strategy_id == signal.strategy_id and p.command.symbol == signal.symbol
            for p in state.pending
        ):
            # Any in-flight command (entry OR exit sequence) means the book
            # is about to change: intents must see the post-resolution book,
            # or an opposite signal becomes a parallel OPEN instead of a
            # flip, and a repeated one duplicates the CLOSE + reversal.
            # Held signals resolve when the slot's queue drains.
            state.deferred.setdefault(slot, []).append(signal)
            return
        mark = state.marking_tick
        assert mark is not None
        entry_price = (
            mark.ask if signal.desired_direction is PositionDirection.LONG else mark.bid
        )
        sizing = SizingInput(
            equity=self._equity(state, w.simulator),
            max_risk_per_trade_pct=self._risk_config.max_risk_per_trade_pct,
            pip_size=self._spec.pip_size,
            quote_currency=self._spec.quote_currency,
            volume_step=self._spec.volume_step,
            entry_price=entry_price,
        )
        self._process_intents(
            state, w, signal, w.portfolio.intents_from_signal(signal, sizing), tick
        )

    def _process_intents(
        self,
        state: _RunState,
        w: _Wiring,
        signal: StrategySignal,
        intents: list[PositionIntent],
        tick: Tick,
    ) -> None:
        if not intents:
            return
        head, rest = intents[0], intents[1:]
        decision = w.risk.evaluate(
            head, self._pretrade_context(state, w, signal, head, tick)
        )
        if not decision.approved:
            state.risk_rejections.append((w.clock.now(), tuple(decision.reject_codes)))
            self._process_intents(state, w, signal, rest, tick)
            return
        if head.action in (PositionAction.OPEN, PositionAction.INCREASE):
            assert decision.approved_quantity is not None
            command = w.oms.command_for_entry(
                intent=head, symbol=head.symbol, quantity=decision.approved_quantity
            )
            state.pending.append(
                _Pending(
                    command=command,
                    strategy_id=head.strategy_id,
                    action=head.action.value,
                    kind="ENTRY",
                )
            )
            self._process_intents(state, w, signal, rest, tick)
            return
        # CLOSE/REDUCE targets every ticket the strategy holds; the follow-up
        # intents (a flip's reversal OPEN) wait until all legs resolved.
        tickets = list(state.open_tickets.get((head.strategy_id, head.symbol), []))
        if not tickets:
            # Already closed (e.g. broker-side protection): NOOP, never a
            # reversal order; follow-ups proceed immediately.
            self._process_intents(state, w, signal, rest, tick)
            return
        barrier = (
            _Barrier(
                tickets=tickets,
                outstanding=len(tickets),
                follow_up=rest,
                signal=signal,
            )
            if rest
            else None
        )
        for ticket in tickets:
            command = w.oms.command_for_hedging_exit(intent=head, ticket=ticket)
            if command is None:
                # Fresh select found nothing: this leg is already flat.
                self._barrier_step(state, w, barrier, tick)
                continue
            state.pending.append(
                _Pending(
                    command=command,
                    strategy_id=head.strategy_id,
                    action=head.action.value,
                    kind="EXIT",
                    barrier=barrier,
                )
            )

    def _barrier_step(
        self, state: _RunState, w: _Wiring, barrier: _Barrier | None, tick: Tick
    ) -> None:
        if barrier is None:
            return
        barrier.outstanding -= 1
        if barrier.outstanding > 0 or not barrier.follow_up:
            return
        # Resolution alone is not flatness: a partial fill or a rejected leg
        # leaves its ticket open, and the reversal must then stay withheld.
        if any(w.simulator.position(t) is not None for t in barrier.tickets):
            return
        self._process_intents(state, w, barrier.signal, barrier.follow_up, tick)

    def _apply_pending(self, state: _RunState, w: _Wiring, tick: Tick) -> None:
        """Fill every pending command whose latency window the clock has
        reached, on THIS tick only — state never changes ahead of the clock.
        Loops because a resolved close leg can enqueue a zero-latency
        follow-up that is itself executable on the same tick.

        Each command leaves the queue immediately before its own fill, never
        as a batch up front: the queue is what tells a held signal that legs
        are still in flight, and what reserves capacity for approved-but-
        unfilled entries. Emptying it early lets a multi-ticket exit release
        held signals — and size a reversal — as if it had already finished."""
        while True:
            ready = [
                p
                for p in state.pending
                if tick.known_time >= w.simulator.executable_from(p.command)
                and tick.time >= w.simulator.executable_from(p.command)
            ]
            if not ready:
                return
            for p in ready:
                state.pending.remove(p)
                self._fill_pending(state, w, p, tick)

    def _fill_pending(
        self, state: _RunState, w: _Wiring, pending: _Pending, tick: Tick
    ) -> None:
        if pending.kind == "EXIT":
            ticket = pending.command.broker_position_ticket
            assert ticket is not None
            held = w.simulator.position(ticket)
            if held is None:
                # Closed while queued (broker-side protection): NOOP, never
                # a reversal.
                self._barrier_step(state, w, pending.barrier, tick)
                self._release_deferred(state, w, pending.strategy_id, tick)
                return
            result = w.simulator.submit(pending.command, [tick])
            if result.fill is None:
                state.rejected_commands += 1
                self._barrier_step(state, w, pending.barrier, tick)
                self._release_deferred(state, w, pending.strategy_id, tick)
                return
            fill = result.fill
            self._settle_close(
                state,
                w,
                ticket=ticket,
                direction=held.direction,
                quantity=fill.quantity,
                price=fill.price,
                at=fill.broker_time,
                mid=tick.mid,
                action=pending.action,
                side=fill.side,
                origin=fill.origin.value,
            )
            state.snapshots.append(self._snapshot(state, w.simulator, w.clock.now()))
            self._barrier_step(state, w, pending.barrier, tick)
            self._release_deferred(state, w, pending.strategy_id, tick)
            return

        result = w.simulator.submit(pending.command, [tick])
        if result.fill is None or result.position is None:
            state.rejected_commands += 1
            self._release_deferred(state, w, pending.strategy_id, tick)
            return
        fill = result.fill
        ticket = result.position.position_id
        strategy_id = pending.strategy_id
        state.ticket_owner[ticket] = strategy_id
        state.entry_price[ticket] = fill.price
        state.entry_mid[ticket] = tick.mid
        state.open_tickets.setdefault((strategy_id, self._spec.symbol), []).append(ticket)
        w.ledger.apply_fill(
            strategy_id, self._spec.symbol, fill.side, fill.quantity, fill.price
        )
        state.fills.append(
            FillRecord(
                at=fill.broker_time,
                strategy_id=strategy_id,
                action=pending.action,
                side=fill.side.value,
                direction=result.position.direction.value,
                quantity=fill.quantity,
                price=fill.price,
                mid=tick.mid,
                origin=fill.origin.value,
            )
        )
        state.snapshots.append(self._snapshot(state, w.simulator, w.clock.now()))
        self._release_deferred(state, w, strategy_id, tick)

    def _settle_close(
        self,
        state: _RunState,
        w: _Wiring,
        *,
        ticket: str,
        direction: PositionDirection,
        quantity: Decimal,
        price: Decimal,
        at: datetime,
        mid: Decimal,
        action: str,
        side: ExecutionSide,
        origin: str,
    ) -> None:
        strategy_id = state.ticket_owner[ticket]
        entry = state.entry_price[ticket]
        state.realized += signed_pnl(direction, entry, price, quantity)
        state.gross_mid_closed += signed_pnl(
            direction, state.entry_mid[ticket], mid, quantity
        )
        w.ledger.apply_fill(strategy_id, self._spec.symbol, side, quantity, price)
        state.fills.append(
            FillRecord(
                at=at,
                strategy_id=strategy_id,
                action=action,
                side=side.value,
                direction=direction.value,
                quantity=quantity,
                price=price,
                mid=mid,
                origin=origin,
            )
        )
        # A partially filled exit leaves the remainder on the book: keep its
        # attribution (entry basis unchanged) so a later protection fill or
        # the end-of-run marking still resolves the ticket.
        if w.simulator.position(ticket) is None:
            slot = (strategy_id, self._spec.symbol)
            tickets = state.open_tickets.get(slot, [])
            if ticket in tickets:
                tickets.remove(ticket)
            if not tickets:
                state.open_tickets.pop(slot, None)
            state.ticket_owner.pop(ticket, None)
            state.entry_price.pop(ticket, None)
            state.entry_mid.pop(ticket, None)

    def _release_deferred(
        self, state: _RunState, w: _Wiring, strategy_id: str, tick: Tick
    ) -> None:
        released = state.deferred.pop((strategy_id, self._spec.symbol), [])
        for signal in released:
            self._process_signal(state, w, signal, tick=tick)

    def _pending_entry_load(
        self, state: _RunState, symbol: str
    ) -> tuple[int, Decimal, Decimal]:
        """(count, signed units, gross units) of queued entry commands: an
        approved-but-unfilled entry already reserves its position slot and
        exposure, or several signals would share the same free capacity."""
        count = 0
        signed = Decimal(0)
        gross = Decimal(0)
        for p in state.pending:
            if p.kind != "ENTRY" or p.command.symbol != symbol:
                continue
            count += 1
            quantity = p.command.quantity
            signed += (
                quantity if p.command.side is ExecutionSide.BUY else -quantity
            )
            gross += quantity
        return count, signed, gross

    def _pretrade_context(
        self,
        state: _RunState,
        w: _Wiring,
        signal: StrategySignal,
        intent: PositionIntent,
        tick: Tick,
    ) -> PreTradeContext:
        symbol = signal.symbol
        pending_count, pending_signed, pending_gross = self._pending_entry_load(
            state, symbol
        )
        return PreTradeContext(
            now=w.clock.now(),
            execution_enabled=True,
            broker_connected=True,
            account_reconciled=True,
            quote=state.marking_tick if state.marking_tick is not None else tick,
            instrument=self._spec,
            account=self._snapshot(state, w.simulator, w.clock.now()),
            snapshots=state.snapshots,
            symbol_open_positions_count=len(w.simulator.open_positions(symbol))
            + pending_count,
            # 単一銘柄 timeline の現行 engine では portfolio 全体 = この銘柄。
            portfolio_open_positions_count=len(w.simulator.open_positions())
            + pending_count,
            symbol_exposure_units=w.broker.net_exposure(symbol) + pending_signed,
            # Graded per horizon against the same calendar live uses. A run
            # given no calendar falls back to the configured default, which is
            # what "no schedule is known" means — not "no event is near".
            event_mode=self._event_mode(w, w.clock.now()),
            kill_switch=KillSwitchLevel.NONE,
            unknown_commands=0,
            account_mode=self._mode,
            symbol_gross_exposure_units=w.broker.gross_exposure(symbol)
            + pending_gross,
            # backtest は live 昇格前の pair の検証こそが目的なので、
            # per-instrument の trading 許可では gate しない（設計書 §30）。
            instrument_trading_enabled=True,
            portfolio_risk=self._portfolio_risk(state, w),
            stop_distance_pips=signal.stop_distance_pips,
            requested_quantity=intent.target_quantity or Decimal(0),
        )

    def _portfolio_risk(self, state: _RunState, w: _Wiring):
        """既存 book の通貨分解。mark は最新の marking tick の mid。

        in-flight（pending）の entry はまだ book に無く、その stop-risk は
        合計に含まれない — 逐次処理の近似で、同時 signal の裁定は Portfolio
        Arbitrator（#64）が引き取る。
        """
        mark = state.marking_tick
        if mark is None:
            return None
        positions = [
            OpenPositionExposure(
                spec=self._spec,
                signed_units=p.quantity
                if p.direction is PositionDirection.LONG
                else -p.quantity,
                mark_price=mark.mid,
                stop_loss_price=p.stop_loss,
            )
            for p in w.simulator.open_positions()
        ]
        return w.exposure.snapshot(positions, w.clock.now())

    def _event_mode(self, w: _Wiring, now: datetime) -> EventRiskMode:
        """The configured default wherever the schedule is unknown: no
        calendar, or a replay period past what the calendar covers. A dataset
        older than the recorded meetings must not grade as quiet."""
        if self._event_risk is None:
            return self._risk_config.event_mode_default
        mode = self._event_risk.mode_for(w.strategy.horizon, now)
        return mode if mode is not None else self._risk_config.event_mode_default

    def _unrealized(self, state: _RunState, simulator: ExecutionSimulator) -> Decimal:
        """Unrealized PnL of the open book, marked at the executable side of
        the latest KNOWN broker-time price."""
        tick = state.marking_tick
        if tick is None:
            return Decimal(0)
        total = Decimal(0)
        for p in simulator.open_positions(self._spec.symbol):
            exit_price = simulator.marking_price(p.direction, tick)
            total += signed_pnl(p.direction, p.entry_price, exit_price, p.quantity)
        return total

    def _equity(self, state: _RunState, simulator: ExecutionSimulator) -> Decimal:
        return state.initial_equity + state.realized + self._unrealized(state, simulator)

    def _snapshot(
        self,
        state: _RunState,
        simulator: ExecutionSimulator,
        now: datetime,
    ) -> AccountSnapshot:
        unrealized = self._unrealized(state, simulator)
        equity = state.initial_equity + state.realized + unrealized
        hwm = max(state.high_water_mark, equity)
        return AccountSnapshot(
            observed_at=now,
            balance=state.initial_equity + state.realized,
            equity=equity,
            margin=Decimal(0),
            free_margin=equity,
            margin_level=None,
            unrealized_pnl=unrealized,
            realized_pnl_day=state.realized,
            high_water_mark=hwm,
            drawdown_from_hwm=max(hwm - equity, Decimal(0)),
            broker_connected=True,
        )

    def _result(self, state: _RunState, simulator: ExecutionSimulator) -> BacktestResult:
        unrealized = self._unrealized(state, simulator)
        # Open positions: gross-mid marks entry AND current price at mid (of
        # the latest known broker-time tick), so the mid-vs-actual gap of the
        # entry shows up as execution cost.
        open_gross_mid = Decimal(0)
        for ticket, entry_mid in state.entry_mid.items():
            held = simulator.position(ticket)
            if held is None or state.marking_tick is None:
                continue
            open_gross_mid += signed_pnl(
                held.direction, entry_mid, state.marking_tick.mid, held.quantity
            )
        gross_mid = state.gross_mid_closed + open_gross_mid
        net = state.realized + unrealized
        execution_cost = gross_mid - net

        protection_fills = sum(1 for f in state.fills if f.origin == "PROTECTION")
        metrics = {
            "initial_equity": str(state.initial_equity),
            "realized_pnl": str(state.realized),
            "unrealized_pnl": str(unrealized),
            "net_pnl": str(net),
            "gross_mid_pnl": str(gross_mid),
            "execution_cost": str(execution_cost),
            "max_drawdown": str(state.max_drawdown),
            "final_equity": str(state.initial_equity + net),
            "fills": str(len(state.fills)),
            "protection_fills": str(protection_fills),
            "rejected_commands": str(state.rejected_commands),
            "risk_rejections": str(len(state.risk_rejections)),
            "open_positions_at_end": str(
                len(simulator.open_positions(self._spec.symbol))
            ),
            "pending_commands_at_end": str(len(state.pending)),
        }
        return BacktestResult(
            symbol=self._spec.symbol,
            fills=state.fills,
            equity_curve=state.equity_curve,
            snapshots=state.snapshots,
            risk_rejections=state.risk_rejections,
            rejected_commands=state.rejected_commands,
            metrics=metrics,
        )
