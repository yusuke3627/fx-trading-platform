"""Market data primitives."""
from __future__ import annotations

from datetime import datetime, timedelta
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
    # Broker quote time.
    time: datetime
    # When WE received the quote (late arrival after a reconnect can be much
    # later than `time`). Replay visibility uses this: a price is usable only
    # from the moment it was actually known.
    received_at: datetime | None = None

    @property
    def known_time(self) -> datetime:
        return self.received_at or self.time

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
    # Broker clock: which quotes belong to this candle.
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int = 0
    # Our clock (real UTC): when this system observed the bar complete, and so
    # the earliest time it may be shown to a strategy. Kept apart from `start`
    # because the broker's zone is not ours — see ADR-005.
    known_at: datetime

    @property
    def close_time(self) -> datetime:
        """The bar's end on the broker clock. Every field exists only once the
        bar has closed, so no quote after this instant belongs to it. This is
        not the visibility instant — that is `known_at`."""
        return self.start + timedelta(seconds=TIMEFRAME_SECONDS[self.timeframe])
