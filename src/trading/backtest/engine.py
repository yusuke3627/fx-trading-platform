"""Vertical-slice backtest engine.

Wires the full order lifecycle over recorded/synthetic ticks:

    Tick -> ReplayClock -> Strategy -> PositionIntent -> RiskDecision
         -> ExecutionCommand -> ExecutionSimulator -> Fill
         -> VirtualPositionLedger -> AccountSnapshot -> metrics

The goal of this slice is reproducing ONE order exactly, not strategy alpha:
the acceptance criteria are determinism (same dataset + config + seed ->
identical fills, PnL and metrics) and cost sensitivity (worse spread/slippage
must worsen net PnL).

Simplifications vs live, by design of the slice: fills are applied
synchronously at decision time (no outbox/worker asynchronicity) with the
simulator's fill timestamp recorded, there is no swap accrual, and open
positions at the end of the replay are marked to market instead of being
force-closed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading.backtest.clock import Clock, ReplayClock
from trading.backtest.costs import CostModel
from trading.backtest.replay import ReplayEngine
from trading.backtest.simulator import ExecutionSimulator
from trading.data.market import InMemoryMarketData
from trading.domain.account import AccountMode, AccountSnapshot
from trading.domain.event import EventEnvelope
from trading.domain.instrument import InstrumentSpec
from trading.domain.intent import PositionIntent
from trading.domain.market import Bar, Tick
from trading.domain.order import ExecutionSide
from trading.domain.position import BrokerPosition, PositionAction, PositionDirection
from trading.domain.risk import EventRiskMode, KillSwitchLevel
from trading.domain.signal import StrategySignal
from trading.indicators import IndicatorService
from trading.intelligence.features import InMemoryFeatureStore
from trading.intelligence.regime import RuleBasedRegimeService
from trading.oms.service import OMSService
from trading.portfolio.manager import PortfolioManager, SizingInput
from trading.portfolio.virtual_ledger import VirtualPositionLedger
from trading.risk.engine import PreTradeContext, RiskConfig, RiskEngine
from trading.strategy.base import (
    Strategy,
    StrategyConfig,
    StrategyContext,
    StrategyHorizon,
)

ENGINE_VERSION = "0.1.0"


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
    risk_rejections: list[tuple[datetime, tuple[str, ...]]]
    rejected_commands: int
    metrics: dict[str, str]


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
    # ticket -> owning strategy / entry marks for PnL attribution.
    ticket_owner: dict[str, str] = field(default_factory=dict)
    entry_price: dict[str, Decimal] = field(default_factory=dict)
    entry_mid: dict[str, Decimal] = field(default_factory=dict)
    open_ticket: dict[tuple[str, str], str] = field(default_factory=dict)
    gross_mid_closed: Decimal = Decimal(0)


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
    market: InMemoryMarketData
    simulator: ExecutionSimulator
    broker: SimulatedBroker
    oms: OMSService
    ledger: VirtualPositionLedger
    portfolio: PortfolioManager
    risk: RiskEngine
    context: StrategyContext


class BacktestEngine:
    def __init__(
        self,
        *,
        risk_config: RiskConfig,
        spec: InstrumentSpec,
        costs: CostModel,
        seed: int,
        strategy: Strategy,
        strategy_config: StrategyConfig,
        initial_equity: Decimal = Decimal(1_000_000),
        account_mode: AccountMode = AccountMode.HEDGING,
    ) -> None:
        if account_mode is not AccountMode.HEDGING:
            raise ValueError("the vertical slice runs on HEDGING only")
        self._risk_config = risk_config
        self._spec = spec
        self._costs = costs
        self._seed = seed
        self._strategy = strategy
        self._strategy_config = strategy_config
        self._initial_equity = initial_equity
        self._mode = account_mode

    def run(self, ticks: list[Tick]) -> BacktestResult:
        symbol = self._spec.symbol
        ordered = sorted(ticks, key=lambda t: t.known_time)
        if not ordered:
            raise ValueError("backtest requires at least one tick")

        w = self._wire(ordered[0].known_time - timedelta(seconds=1))
        state = _RunState(initial_equity=self._initial_equity)
        state.high_water_mark = self._initial_equity
        state.snapshots.append(self._snapshot(state, w.simulator, ordered[0], w.clock.now()))
        mid_by_time = {t.time: t.mid for t in ordered}
        cursor = 0

        def handle(item: EventEnvelope | Tick | Bar) -> None:
            nonlocal cursor
            assert isinstance(item, Tick)
            index = cursor
            cursor += 1
            w.market.add_tick(item)

            # Broker-side protection evaluates the price before the decision
            # layers see it (broker events precede strategy evaluation).
            for position in w.simulator.open_positions(symbol):
                fill = w.simulator.check_protection(position, item)
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
                    mid=mid_by_time[fill.broker_time],
                    action="PROTECTION_CLOSE",
                    side=fill.side,
                    origin=fill.origin.value,
                )
                state.snapshots.append(
                    self._snapshot(state, w.simulator, item, w.clock.now())
                )

            envelope = EventEnvelope(
                event_id=uuid4(),
                event_type="market.tick",
                source="replay",
                retrieved_at=item.known_time,
                known_at=item.known_time,
            )
            signals = runner_loop.run(self._strategy.on_event(envelope, w.context))
            for signal in signals:
                self._process_signal(
                    state, w, signal, tick=item, remaining=ordered[index:],
                    mid_by_time=mid_by_time,
                )

            equity = self._equity(state, w.simulator, item)
            state.high_water_mark = max(state.high_water_mark, equity)
            state.equity_curve.append((w.clock.now(), equity))

        with asyncio.Runner() as runner_loop:
            ReplayEngine(w.clock).run(ordered, handle)

        return self._result(state, w.simulator, ordered[-1])

    def _wire(self, start: datetime) -> _Wiring:
        clock = ReplayClock(start)
        market = InMemoryMarketData(clock)
        market.set_instrument(self._spec)
        simulator = ExecutionSimulator(self._costs, self._spec, self._seed, self._mode)
        broker = SimulatedBroker(simulator, clock)
        ledger = VirtualPositionLedger(clock)
        features = InMemoryFeatureStore()
        return _Wiring(
            clock=clock,
            market=market,
            simulator=simulator,
            broker=broker,
            oms=OMSService(account_mode=self._mode, broker=broker, clock=clock),
            ledger=ledger,
            portfolio=PortfolioManager(ledger, clock),
            risk=RiskEngine(self._risk_config, clock),
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
        self,
        state: _RunState,
        w: _Wiring,
        signal: StrategySignal,
        *,
        tick: Tick,
        remaining: list[Tick],
        mid_by_time: dict[datetime, Decimal],
    ) -> None:
        entry_price = (
            tick.ask if signal.desired_direction is PositionDirection.LONG else tick.bid
        )
        sizing = SizingInput(
            equity=self._equity(state, w.simulator, tick),
            max_risk_per_trade_pct=self._risk_config.max_risk_per_trade_pct,
            pip_size=self._spec.pip_size,
            volume_step=self._spec.volume_step,
            entry_price=entry_price,
        )
        for intent in w.portfolio.intents_from_signal(signal, sizing):
            decision = w.risk.evaluate(
                intent, self._pretrade_context(state, w, signal, intent, tick)
            )
            if not decision.approved:
                state.risk_rejections.append(
                    (w.clock.now(), tuple(decision.reject_codes))
                )
                continue
            if intent.action in (PositionAction.OPEN, PositionAction.INCREASE):
                self._execute_entry(state, w, intent, decision.approved_quantity,
                                    tick=tick, remaining=remaining, mid_by_time=mid_by_time)
            else:
                self._execute_exit(state, w, intent, tick=tick, remaining=remaining,
                                   mid_by_time=mid_by_time)

    def _execute_entry(
        self,
        state: _RunState,
        w: _Wiring,
        intent: PositionIntent,
        approved_quantity: Decimal | None,
        *,
        tick: Tick,
        remaining: list[Tick],
        mid_by_time: dict[datetime, Decimal],
    ) -> None:
        assert approved_quantity is not None
        symbol = intent.symbol
        command = w.oms.command_for_entry(
            intent=intent, symbol=symbol, quantity=approved_quantity
        )
        result = w.simulator.submit(command, remaining)
        if result.fill is None or result.position is None:
            state.rejected_commands += 1
            return
        fill = result.fill
        ticket = result.position.position_id
        # open_ticket targets the LATEST entry for flip closes; an earlier
        # ticket left open (e.g. a partially filled exit) stays fully tracked
        # through the per-ticket maps: protection fills and end-of-run
        # marking attribute against ticket_owner/entry_*, not this slot.
        state.ticket_owner[ticket] = intent.strategy_id
        state.entry_price[ticket] = fill.price
        state.entry_mid[ticket] = mid_by_time[fill.broker_time]
        state.open_ticket[(intent.strategy_id, symbol)] = ticket
        w.ledger.apply_fill(
            intent.strategy_id, symbol, fill.side, fill.quantity, fill.price
        )
        state.fills.append(
            FillRecord(
                at=fill.broker_time,
                strategy_id=intent.strategy_id,
                action=intent.action.value,
                side=fill.side.value,
                direction=intent.direction.value,
                quantity=fill.quantity,
                price=fill.price,
                mid=mid_by_time[fill.broker_time],
                origin=fill.origin.value,
            )
        )
        state.snapshots.append(self._snapshot(state, w.simulator, tick, w.clock.now()))

    def _execute_exit(
        self,
        state: _RunState,
        w: _Wiring,
        intent: PositionIntent,
        *,
        tick: Tick,
        remaining: list[Tick],
        mid_by_time: dict[datetime, Decimal],
    ) -> None:
        symbol = intent.symbol
        ticket = state.open_ticket.get((intent.strategy_id, symbol))
        if ticket is None:
            # Already closed (e.g. broker-side protection): NOOP, never a
            # reversal order.
            return
        held = w.simulator.position(ticket)
        command = w.oms.command_for_hedging_exit(intent=intent, ticket=ticket)
        if command is None or held is None:
            return
        result = w.simulator.submit(command, remaining)
        if result.fill is None:
            state.rejected_commands += 1
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
            mid=mid_by_time[fill.broker_time],
            action=intent.action.value,
            side=fill.side,
            origin=fill.origin.value,
        )
        state.snapshots.append(self._snapshot(state, w.simulator, tick, w.clock.now()))

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
            if state.open_ticket.get((strategy_id, self._spec.symbol)) == ticket:
                state.open_ticket.pop((strategy_id, self._spec.symbol))
            state.ticket_owner.pop(ticket, None)
            state.entry_price.pop(ticket, None)
            state.entry_mid.pop(ticket, None)

    def _pretrade_context(
        self,
        state: _RunState,
        w: _Wiring,
        signal: StrategySignal,
        intent: PositionIntent,
        tick: Tick,
    ) -> PreTradeContext:
        symbol = signal.symbol
        return PreTradeContext(
            now=w.clock.now(),
            execution_enabled=True,
            broker_connected=True,
            account_reconciled=True,
            quote=tick,
            instrument=self._spec,
            account=self._snapshot(state, w.simulator, tick, w.clock.now()),
            snapshots=state.snapshots,
            open_positions_count=len(w.simulator.open_positions(symbol)),
            symbol_exposure_units=w.broker.net_exposure(symbol),
            event_mode=EventRiskMode.NORMAL,
            kill_switch=KillSwitchLevel.NONE,
            unknown_commands=0,
            account_mode=self._mode,
            symbol_gross_exposure_units=w.broker.gross_exposure(symbol),
            stop_distance_pips=signal.stop_distance_pips,
            requested_quantity=intent.target_quantity or Decimal(0),
        )

    def _unrealized(self, simulator: ExecutionSimulator, tick: Tick) -> Decimal:
        """Unrealized PnL of the open book, marked at the executable side."""
        total = Decimal(0)
        for p in simulator.open_positions(self._spec.symbol):
            exit_price = tick.bid if p.direction is PositionDirection.LONG else tick.ask
            total += signed_pnl(p.direction, p.entry_price, exit_price, p.quantity)
        return total

    def _equity(
        self, state: _RunState, simulator: ExecutionSimulator, tick: Tick
    ) -> Decimal:
        return state.initial_equity + state.realized + self._unrealized(simulator, tick)

    def _snapshot(
        self,
        state: _RunState,
        simulator: ExecutionSimulator,
        tick: Tick,
        now: datetime,
    ) -> AccountSnapshot:
        unrealized = self._unrealized(simulator, tick)
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

    def _result(
        self, state: _RunState, simulator: ExecutionSimulator, last_tick: Tick
    ) -> BacktestResult:
        unrealized = self._unrealized(simulator, last_tick)
        # Open positions: gross-mid marks entry AND current price at mid, so
        # the mid-vs-actual gap of the entry shows up as execution cost.
        open_gross_mid = Decimal(0)
        for ticket, entry_mid in state.entry_mid.items():
            held = simulator.position(ticket)
            if held is None:
                continue
            open_gross_mid += signed_pnl(
                held.direction, entry_mid, last_tick.mid, held.quantity
            )
        gross_mid = state.gross_mid_closed + open_gross_mid
        net = state.realized + unrealized
        execution_cost = gross_mid - net

        peak = state.initial_equity
        max_drawdown = Decimal(0)
        for _, equity in state.equity_curve:
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)

        protection_fills = sum(1 for f in state.fills if f.origin == "PROTECTION")
        metrics = {
            "initial_equity": str(state.initial_equity),
            "realized_pnl": str(state.realized),
            "unrealized_pnl": str(unrealized),
            "net_pnl": str(net),
            "gross_mid_pnl": str(gross_mid),
            "execution_cost": str(execution_cost),
            "max_drawdown": str(max_drawdown),
            "final_equity": str(state.initial_equity + net),
            "fills": str(len(state.fills)),
            "protection_fills": str(protection_fills),
            "rejected_commands": str(state.rejected_commands),
            "risk_rejections": str(len(state.risk_rejections)),
            "open_positions_at_end": str(
                len(simulator.open_positions(self._spec.symbol))
            ),
        }
        return BacktestResult(
            symbol=self._spec.symbol,
            fills=state.fills,
            equity_curve=state.equity_curve,
            risk_rejections=state.risk_rejections,
            rejected_commands=state.rejected_commands,
            metrics=metrics,
        )
