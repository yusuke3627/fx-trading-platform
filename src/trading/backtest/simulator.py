"""Execution simulator.

Fills execution commands against observed bid/ask tick streams with latency,
slippage, rejects, partial fills and broker-side protection (SL/TP) fills —
including stop-through in stressed scenarios. Deterministic under a seed.
"""
from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from trading.backtest.costs import CostModel
from trading.domain.fill import Fill, FillOrigin, ProtectionReason
from trading.domain.instrument import InstrumentSpec
from trading.domain.market import Tick
from trading.domain.order import ExecutionCommand, ExecutionSide
from trading.domain.position import PositionAction, PositionDirection


@dataclass(frozen=True)
class SimulatedPosition:
    position_id: str
    symbol: str
    direction: PositionDirection
    quantity: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None


@dataclass(frozen=True)
class SimulationResult:
    fill: Fill | None
    rejected: bool
    position: SimulatedPosition | None


class ExecutionSimulator:
    """Holds the simulated broker's position book: exits are validated
    against it exactly like a real broker validates a position ticket."""

    def __init__(self, costs: CostModel, spec: InstrumentSpec, seed: int) -> None:
        self._costs = costs
        self._spec = spec
        self._rng = random.Random(seed)
        self._positions: dict[str, SimulatedPosition] = {}

    def position(self, position_id: str) -> SimulatedPosition | None:
        return self._positions.get(position_id)

    def submit(
        self, command: ExecutionCommand, ticks: Sequence[Tick]
    ) -> SimulationResult:
        """Fill a market command at the first tick after latency.

        OPEN/INCREASE registers a new SimulatedPosition. REDUCE/CLOSE
        re-checks the referenced ticket against the book first: if broker
        protection already removed the position, the order does NOT execute
        (fill=None) — exactly like a real broker rejecting an unknown ticket.
        An exit never fills more than the held quantity, so a queued close
        can never manufacture a reversal.
        """
        if not ticks:
            return SimulationResult(fill=None, rejected=True, position=None)

        if self._rng.random() < self._costs.reject_probability:
            return SimulationResult(fill=None, rejected=True, position=None)

        # Latency runs from the command's creation, not from whatever history
        # happens to sit at the head of the tick window: an order must never
        # fill on ticks that predate it.
        submit_time = command.created_at
        fill_after = submit_time + timedelta(milliseconds=self._costs.latency_ms)
        fill_tick = next((t for t in ticks if t.time >= fill_after), None)
        if fill_tick is None:
            # No market data after the latency window: not filled, rather than
            # optimistically filling at the last observed tick.
            return SimulationResult(fill=None, rejected=True, position=None)

        opening = command.action in (PositionAction.OPEN, PositionAction.INCREASE)
        held: SimulatedPosition | None = None
        if not opening:
            ticket = command.broker_position_ticket
            held = self._positions.get(ticket) if ticket else None
            if held is None:
                # The referenced position no longer exists (e.g. protection
                # closed it first): the order does not execute.
                return SimulationResult(fill=None, rejected=True, position=None)

        price = self._execution_price(fill_tick, command.side)

        quantity = command.quantity
        if self._rng.random() < self._costs.partial_fill_probability:
            quantity = (
                command.quantity / 2 // self._spec.volume_step
            ) * self._spec.volume_step
            if quantity <= 0:
                quantity = command.quantity
        if held is not None:
            quantity = min(quantity, held.quantity)

        ticket_for_fill = (
            command.broker_position_ticket if held is not None else None
        )
        fill = Fill(
            fill_id=uuid4(),
            broker_deal_id=f"sim-{uuid4().hex[:12]}",
            broker_order_id=None,
            broker_position_ticket=ticket_for_fill,
            broker_position_identifier=ticket_for_fill,
            execution_command_id=command.command_id,
            origin=FillOrigin.COMMAND,
            protection_reason=None,
            side=command.side,
            quantity=quantity,
            price=price,
            broker_time=fill_tick.time,
            received_at=fill_tick.time,
        )

        if opening:
            position = SimulatedPosition(
                position_id=f"simpos-{uuid4().hex[:12]}",
                symbol=command.symbol,
                direction=command.direction,
                quantity=quantity,
                entry_price=price,
                stop_loss=command.stop_loss_price,
                take_profit=command.take_profit_price,
            )
            self._positions[position.position_id] = position
            return SimulationResult(fill=fill, rejected=False, position=position)

        assert held is not None
        remaining = held.quantity - quantity
        if remaining > 0:
            updated = replace(held, quantity=remaining)
            self._positions[held.position_id] = updated
            return SimulationResult(fill=fill, rejected=False, position=updated)
        del self._positions[held.position_id]
        return SimulationResult(fill=fill, rejected=False, position=None)

    def check_protection(
        self, position: SimulatedPosition, tick: Tick
    ) -> Fill | None:
        """Broker-side SL/TP evaluation on each tick. LONG exits at bid,
        SHORT at ask; stop-through adds adverse pips in stressed scenarios.
        A triggered protection removes the position from the book, so a
        queued system exit for the same ticket can no longer execute and the
        same protection can never fire twice. Evaluation and the fill both
        use the book's LATEST state — a caller holding the object from OPEN
        must not close pre-reduction quantity."""
        held = self._positions.get(position.position_id)
        if held is None:
            return None
        position = held
        pip = self._spec.pip_size
        through = Decimal(str(self._costs.stop_through_pips)) * pip

        if position.direction is PositionDirection.LONG:
            exit_side = ExecutionSide.SELL
            market = self._stressed_bid(tick)
            if position.stop_loss is not None and market <= position.stop_loss:
                return self._protection_fill(
                    position, exit_side, market - through, tick, ProtectionReason.STOP_LOSS
                )
            if position.take_profit is not None and market >= position.take_profit:
                return self._protection_fill(
                    position, exit_side, market, tick, ProtectionReason.TAKE_PROFIT
                )
        else:
            exit_side = ExecutionSide.BUY
            market = self._stressed_ask(tick)
            if position.stop_loss is not None and market >= position.stop_loss:
                return self._protection_fill(
                    position, exit_side, market + through, tick, ProtectionReason.STOP_LOSS
                )
            if position.take_profit is not None and market <= position.take_profit:
                return self._protection_fill(
                    position, exit_side, market, tick, ProtectionReason.TAKE_PROFIT
                )
        return None

    def _protection_fill(
        self,
        position: SimulatedPosition,
        side: ExecutionSide,
        price: Decimal,
        tick: Tick,
        reason: ProtectionReason,
    ) -> Fill:
        self._positions.pop(position.position_id, None)
        return Fill(
            fill_id=uuid4(),
            broker_deal_id=f"sim-{uuid4().hex[:12]}",
            broker_order_id=None,
            broker_position_ticket=position.position_id,
            broker_position_identifier=position.position_id,
            execution_command_id=None,
            origin=FillOrigin.PROTECTION,
            protection_reason=reason,
            side=side,
            quantity=position.quantity,
            price=price,
            broker_time=tick.time,
            received_at=tick.time,
        )

    def _execution_price(self, tick: Tick, side: ExecutionSide) -> Decimal:
        slippage_pips = abs(self._rng.gauss(0.0, self._costs.slippage_sigma_pips))
        if self._rng.random() < self._costs.tail_probability:
            slippage_pips += self._rng.uniform(0.0, self._costs.tail_max_pips)
        slippage = Decimal(str(round(slippage_pips, 3))) * self._spec.pip_size
        if side is ExecutionSide.BUY:
            return self._stressed_ask(tick) + slippage
        return self._stressed_bid(tick) - slippage

    def _stressed_spread(self, tick: Tick) -> Decimal:
        return tick.spread * Decimal(str(self._costs.spread_multiplier))

    def _stressed_ask(self, tick: Tick) -> Decimal:
        return tick.mid + self._stressed_spread(tick) / 2

    def _stressed_bid(self, tick: Tick) -> Decimal:
        return tick.mid - self._stressed_spread(tick) / 2
