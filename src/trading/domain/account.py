"""Account model.

The account mode (netting/hedging) is machine-detected from MT5 at startup and
compared against configuration; a mismatch disables execution. Human memory of
"it is probably hedging" is never trusted.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class AccountMode(StrEnum):
    NETTING = "NETTING"
    EXCHANGE = "EXCHANGE"
    HEDGING = "HEDGING"


class AccountTradeMode(StrEnum):
    DEMO = "DEMO"
    CONTEST = "CONTEST"
    REAL = "REAL"


class AccountSnapshot(BaseModel):
    """Persisted equity observation; source of truth for daily / rolling /
    high-water-mark risk calculations."""

    model_config = ConfigDict(frozen=True)

    observed_at: datetime

    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    margin_level: Decimal | None = None

    unrealized_pnl: Decimal
    realized_pnl_day: Decimal

    high_water_mark: Decimal
    drawdown_from_hwm: Decimal

    broker_connected: bool
