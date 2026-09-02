"""OMS request rate limits."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field


class RateLimitConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    per_symbol_requests_per_second: int = Field(default=5, ge=1)
    market_entries_per_second: int = Field(default=1, ge=1)


class RateLimiter:
    WINDOW = timedelta(seconds=1)

    def __init__(self, config: RateLimitConfig) -> None:
        self._config = config
        self._symbol_requests: dict[str, deque[datetime]] = {}
        self._market_entries: deque[datetime] = deque()

    def allows(self, symbol: str, *, market_entry: bool, now: datetime) -> bool:
        requests = self._symbol_requests.setdefault(symbol, deque())
        self._discard_expired(requests, now)
        self._discard_expired(self._market_entries, now)

        if len(requests) >= self._config.per_symbol_requests_per_second:
            return False
        return not market_entry or (
            len(self._market_entries) < self._config.market_entries_per_second
        )

    def record(self, symbol: str, *, market_entry: bool, now: datetime) -> None:
        requests = self._symbol_requests.setdefault(symbol, deque())
        self._discard_expired(requests, now)
        self._discard_expired(self._market_entries, now)

        requests.append(now)
        if market_entry:
            self._market_entries.append(now)

    def _discard_expired(self, timestamps: deque[datetime], now: datetime) -> None:
        cutoff = now - self.WINDOW
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
