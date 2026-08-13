"""Scheduled event risk.

Consecutive central-bank meetings are treated as one independent risk state
(e.g. DUAL_CENTRAL_BANK_CLUSTER), not as the sum of individual event risks.
Thresholds and windows live in YAML configuration, not in the spec text.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from trading.domain.risk import EventRiskMode
from trading.strategy.base import StrategyHorizon

_SEVERITY = [EventRiskMode.NORMAL, EventRiskMode.REDUCED, EventRiskMode.HALT]


class EventRiskWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    first_event_at: datetime
    last_event_at: datetime
    pre_hours: int
    post_hours: int
    actions: dict[StrategyHorizon, EventRiskMode] = Field(default_factory=dict)

    def active_at(self, now: datetime) -> bool:
        start = self.first_event_at - timedelta(hours=self.pre_hours)
        end = self.last_event_at + timedelta(hours=self.post_hours)
        return start <= now <= end


class EventRiskCalendar:
    def __init__(self, windows: list[EventRiskWindow]) -> None:
        self._windows = windows

    def mode_for(self, horizon: StrategyHorizon, now: datetime) -> EventRiskMode:
        """Most severe mode across all active windows; NORMAL when none."""
        mode = EventRiskMode.NORMAL
        for w in self._windows:
            if not w.active_at(now):
                continue
            action = w.actions.get(horizon, EventRiskMode.NORMAL)
            if _SEVERITY.index(action) > _SEVERITY.index(mode):
                mode = action
        return mode
