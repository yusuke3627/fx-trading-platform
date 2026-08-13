"""Market data primitives."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

TIMEFRAME_SECONDS: dict[str, int] = {
    "1s": 1,
    "5s": 5,
    "15s": 15,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


class Tick(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    bid: Decimal
    ask: Decimal
    time: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


class Bar(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int = 0
