"""Execution command model and OMS states."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trading.domain.position import PositionAction, PositionDirection


class ExecutionSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class CommandState(StrEnum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    READY = "READY"
    # CLAIMED: a worker holds the DB row; no broker side effect exists yet.
    CLAIMED = "CLAIMED"
    # SUBMITTING: the broker call has started; a side effect may exist.
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"

    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    # UNKNOWN: broker truth unconfirmed. Never blindly retried; resolved only
    # through reconciliation against broker history.
    UNKNOWN = "UNKNOWN"


def execution_side(direction: PositionDirection, action: PositionAction) -> ExecutionSide:
    """Map a position-level intent to the broker order side."""
    opening = action in (PositionAction.OPEN, PositionAction.INCREASE)
    if direction is PositionDirection.LONG:
        return ExecutionSide.BUY if opening else ExecutionSide.SELL
    return ExecutionSide.SELL if opening else ExecutionSide.BUY


class ExecutionCommand(BaseModel):
    """One broker order attempt. State changes produce new copies."""

    model_config = ConfigDict(frozen=True)

    command_id: UUID
    intent_id: UUID | None = None
    idempotency_key: str

    symbol: str
    side: ExecutionSide
    action: PositionAction
    direction: PositionDirection
    quantity: Decimal

    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None

    # Required for REDUCE/CLOSE on hedging accounts: exit is never a naked
    # opposite market order.
    broker_position_ticket: str | None = None

    state: CommandState = CommandState.CREATED

    claimed_by: str | None = None
    claimed_at: datetime | None = None
    claim_expires_at: datetime | None = None
    submitting_at: datetime | None = None
    broker_request_started_at: datetime | None = None

    created_at: datetime
