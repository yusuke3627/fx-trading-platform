"""Position intent.

Strategies (via the portfolio layer) express *desired position changes*, never
broker orders. "Increase my SHORT to 20,000 units" is an intent; "send SELL
10,000 to MT5" is OMS/execution territory.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from trading.domain.position import PositionAction, PositionDirection


class ProtectionSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    stop_loss_price: Decimal
    take_profit_price: Decimal | None = None

    maximum_unprotected_seconds: int

    source: Literal["STRATEGY", "RISK_OVERRIDE", "EMERGENCY"]


class PositionIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent_id: UUID
    strategy_id: str
    strategy_version: str

    symbol: str

    action: PositionAction
    direction: PositionDirection

    target_quantity: Decimal | None = None
    delta_quantity: Decimal | None = None

    protection: ProtectionSpec | None = None

    reason_codes: list[str] = Field(default_factory=list)

    generated_at: datetime
