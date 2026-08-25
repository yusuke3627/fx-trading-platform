"""Scheduled event risk.

Consecutive central-bank meetings are treated as one independent risk state
(e.g. DUAL_CENTRAL_BANK_CLUSTER), not as the sum of individual event risks.
Thresholds and windows live in YAML configuration, not in the spec text.

Wiring note: YAML supplies pre/post hours and per-horizon actions
(config.EventRiskWindowSettings); the concrete event datetimes come from the
scheduled-event calendar built by application wiring (vertical slice).
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
    """The scheduled events a run knows about, and the span they describe.

    A calendar speaks only for the period its windows cover. The meeting file
    is maintained by hand and reaches as far as somebody has recorded, so an
    instant past its last window is not a quiet one — it is one nobody has
    written down yet. Answering NORMAL there would turn a gap in the file into
    a statement that nothing is happening, which is how a run ends up trading
    through a decision it simply had not heard of.
    """

    def __init__(self, windows: list[EventRiskWindow]) -> None:
        self._windows = windows
        self._covers = (
            (
                min(w.first_event_at - timedelta(hours=w.pre_hours) for w in windows),
                max(w.last_event_at + timedelta(hours=w.post_hours) for w in windows),
            )
            if windows
            else None
        )

    def covers(self, now: datetime) -> bool:
        """Whether the calendar has anything to say about this instant."""
        if self._covers is None:
            return False
        start, end = self._covers
        return start <= now <= end

    def mode_for(self, horizon: StrategyHorizon, now: datetime) -> EventRiskMode | None:
        """Most severe mode across the active windows, NORMAL when the instant
        is covered and none is active, and None when it is outside the span the
        calendar describes."""
        if not self.covers(now):
            return None
        mode = EventRiskMode.NORMAL
        for w in self._windows:
            if not w.active_at(now):
                continue
            action = w.actions.get(horizon, EventRiskMode.NORMAL)
            if _SEVERITY.index(action) > _SEVERITY.index(mode):
                mode = action
        return mode
