"""Fill model.

A broker-side SL/TP execution is not an untracked fill: if the deal belongs to
a known own position and carries a protection reason, it is accepted as a
PROTECTION-origin fill. "tracked" means command-origin + protection-origin.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trading.domain.order import ExecutionSide


class FillOrigin(StrEnum):
    COMMAND = "COMMAND"
    PROTECTION = "PROTECTION"
    BROKER_EXTERNAL = "BROKER_EXTERNAL"


class ProtectionReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_OUT = "STOP_OUT"


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True)

    fill_id: UUID

    broker_deal_id: str
    broker_order_id: str | None = None

    broker_position_ticket: str | None = None
    broker_position_identifier: str | None = None

    execution_command_id: UUID | None = None

    origin: FillOrigin
    protection_reason: ProtectionReason | None = None

    side: ExecutionSide
    quantity: Decimal
    price: Decimal

    broker_time: datetime
    received_at: datetime


class BrokerDeal(BaseModel):
    """Raw deal observation from the broker, before classification."""

    model_config = ConfigDict(frozen=True)

    broker_deal_id: str
    broker_order_id: str | None = None
    broker_position_ticket: str | None = None
    broker_position_identifier: str | None = None

    reason_code: int | None = None
    protection_reason: ProtectionReason | None = None

    side: ExecutionSide
    quantity: Decimal
    price: Decimal
    broker_time: datetime
