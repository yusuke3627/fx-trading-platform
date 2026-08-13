"""Risk decision model."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class KillSwitchLevel(StrEnum):
    NONE = "NONE"
    HALT_NEW_ORDER = "HALT_NEW_ORDER"
    CLOSE_ONLY = "CLOSE_ONLY"
    EMERGENCY = "EMERGENCY"


class EventRiskMode(StrEnum):
    NORMAL = "NORMAL"
    REDUCED = "REDUCED"
    HALT = "HALT"


class RiskCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    critical: bool = True
    detail: str | None = None


class RiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: UUID
    intent_id: UUID

    approved: bool
    # Risk may approve less than requested (e.g. REDUCED event mode).
    approved_quantity: Decimal | None = None

    checks: list[RiskCheck] = Field(default_factory=list)
    reject_codes: list[str] = Field(default_factory=list)

    decided_at: datetime
