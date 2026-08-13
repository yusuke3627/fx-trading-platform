"""OMS service.

Netting: order quantity is (desired broker net exposure - current broker
exposure), never raw strategy quantity. Hedging: exits always reference the
target position ticket. In both modes exit is never a naked opposite market
order: the broker position is re-selected fresh before any close/reduce, and
a position already closed by broker-side protection results in a NOOP, never
a reversal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from trading.backtest.clock import Clock
from trading.domain.account import AccountMode
from trading.domain.intent import PositionIntent
from trading.domain.order import (
    CommandState,
    ExecutionCommand,
    ExecutionSide,
    execution_side,
)
from trading.domain.position import BrokerPosition, PositionAction, PositionDirection


class NakedExitError(RuntimeError):
    pass


class BrokerPositionReader(Protocol):
    """Fresh broker position lookup (re-select before reading)."""

    def position(self, ticket: str) -> BrokerPosition | None: ...


class ExitPreparation(StrEnum):
    PROCEED = "PROCEED"
    ALREADY_CLOSED = "ALREADY_CLOSED"


@dataclass(frozen=True)
class ExitPlan:
    result: ExitPreparation
    position: BrokerPosition | None = None
    quantity: Decimal | None = None


def execution_delta(desired_net: Decimal, current_net: Decimal) -> Decimal:
    """Signed order quantity that moves broker exposure to the target."""
    return desired_net - current_net


class OMSService:
    def __init__(
        self,
        *,
        account_mode: AccountMode,
        broker: BrokerPositionReader,
        clock: Clock,
    ) -> None:
        self._mode = account_mode
        self._broker = broker
        self._clock = clock

    def command_for_netting(
        self,
        *,
        symbol: str,
        desired_net: Decimal,
        current_net: Decimal,
        intent: PositionIntent,
        volume_step: Decimal,
    ) -> ExecutionCommand | None:
        """Difference-only order for a netting account; None when the delta is
        below one volume step."""
        if self._mode is not AccountMode.NETTING:
            raise RuntimeError("command_for_netting requires a NETTING account")
        delta = execution_delta(desired_net, current_net)
        quantity = abs(delta)
        if quantity < volume_step:
            return None
        side = ExecutionSide.BUY if delta > 0 else ExecutionSide.SELL
        return self._command(intent, symbol=symbol, side=side, quantity=quantity)

    def prepare_exit(self, ticket: str) -> ExitPlan:
        """Fresh position select before any exit. A missing position means it
        was already closed (e.g. broker-side protection): NOOP, never a
        reversal order."""
        position = self._broker.position(ticket)
        if position is None:
            return ExitPlan(result=ExitPreparation.ALREADY_CLOSED)
        return ExitPlan(
            result=ExitPreparation.PROCEED,
            position=position,
            quantity=position.quantity,
        )

    def command_for_hedging_exit(
        self,
        *,
        intent: PositionIntent,
        ticket: str,
        quantity: Decimal | None = None,
    ) -> ExecutionCommand | None:
        """Ticket-referenced REDUCE/CLOSE for a hedging account."""
        if self._mode is not AccountMode.HEDGING:
            raise RuntimeError("command_for_hedging_exit requires a HEDGING account")
        if intent.action not in (PositionAction.REDUCE, PositionAction.CLOSE):
            raise ValueError("exit command requires a REDUCE/CLOSE intent")

        plan = self.prepare_exit(ticket)
        if plan.result is ExitPreparation.ALREADY_CLOSED:
            return None
        assert plan.position is not None

        exit_quantity = quantity if quantity is not None else plan.position.quantity
        # A system exit cannot reverse a position: never exit more than exists.
        exit_quantity = min(exit_quantity, plan.position.quantity)

        return self._command(
            intent,
            symbol=plan.position.symbol,
            side=execution_side(plan.position.direction, intent.action),
            quantity=exit_quantity,
            ticket=ticket,
        )

    def validate_command(self, command: ExecutionCommand) -> None:
        """Structural invariant: on hedging accounts an exit must reference a
        broker position ticket."""
        is_exit = command.action in (PositionAction.REDUCE, PositionAction.CLOSE)
        if (
            is_exit
            and self._mode is AccountMode.HEDGING
            and command.broker_position_ticket is None
        ):
            raise NakedExitError(
                "exit is never a naked market order: broker_position_ticket required"
            )

    def _command(
        self,
        intent: PositionIntent,
        *,
        symbol: str,
        side: ExecutionSide,
        quantity: Decimal,
        ticket: str | None = None,
    ) -> ExecutionCommand:
        now: datetime = self._clock.now()
        command = ExecutionCommand(
            command_id=uuid4(),
            intent_id=intent.intent_id,
            idempotency_key=f"{intent.intent_id}:{intent.action}:{symbol}",
            symbol=symbol,
            side=side,
            action=intent.action,
            direction=intent.direction,
            quantity=quantity,
            stop_loss_price=(
                intent.protection.stop_loss_price if intent.protection else None
            ),
            take_profit_price=(
                intent.protection.take_profit_price if intent.protection else None
            ),
            broker_position_ticket=ticket,
            state=CommandState.CREATED,
            created_at=now,
        )
        self.validate_command(command)
        return command
