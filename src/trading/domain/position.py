"""Position model.

PositionDirection (LONG/SHORT) is a property of a position; ExecutionSide
(BUY/SELL) is a property of an order. Closing a SHORT position is a BUY order,
not a BUY signal.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PositionDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionAction(StrEnum):
    OPEN = "OPEN"
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


class PositionState(StrEnum):
    PENDING_OPEN = "PENDING_OPEN"
    OPEN = "OPEN"
    # OPEN without broker-side stop loss: a CRITICAL state that triggers
    # protection repair, then close + HALT_NEW_ORDER on repair failure.
    OPEN_UNPROTECTED = "OPEN_UNPROTECTED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class VirtualPosition(BaseModel):
    """Per-strategy virtual position snapshot.

    Snapshots are append-only history; the current position is the row with
    MAX(as_of) per (strategy_id, symbol).
    """

    model_config = ConfigDict(frozen=True)

    strategy_id: str
    symbol: str
    direction: PositionDirection
    quantity: Decimal
    average_price: Decimal | None = None
    as_of: datetime

    @property
    def signed_quantity(self) -> Decimal:
        if self.direction is PositionDirection.LONG:
            return self.quantity
        return -self.quantity


class BrokerPosition(BaseModel):
    """Broker-side position observation.

    Both ticket and identifier are stored: under netting, reversals can break
    ticket-only tracking, while POSITION_IDENTIFIER follows the lifecycle.
    """

    model_config = ConfigDict(frozen=True)

    broker_position_ticket: str
    broker_position_identifier: str

    symbol: str
    direction: PositionDirection
    quantity: Decimal
    entry_price: Decimal | None = None

    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None

    observed_at: datetime

    @property
    def protected(self) -> bool:
        return self.stop_loss is not None

    @property
    def signed_quantity(self) -> Decimal:
        if self.direction is PositionDirection.LONG:
            return self.quantity
        return -self.quantity


def net_exposure(positions: Iterable[BrokerPosition | VirtualPosition]) -> Decimal:
    return sum((p.signed_quantity for p in positions), Decimal(0))
