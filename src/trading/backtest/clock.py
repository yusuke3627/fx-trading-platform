"""Clock abstraction.

Strategies must never call datetime.now() directly; they read time from the
injected Clock so the same code runs against SystemClock (live) and
ReplayClock (backtest).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ClockRegressionError(RuntimeError):
    pass


class ReplayClock:
    """Deterministic clock driven by the replay engine. Time never regresses."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ReplayClock requires an aware datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, t: datetime) -> None:
        if t < self._now:
            raise ClockRegressionError(f"replay clock cannot move back: {self._now} -> {t}")
        self._now = t
