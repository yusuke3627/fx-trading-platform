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

    def __init__(
        self,
        windows: list[EventRiskWindow],
        covers: tuple[datetime, datetime] | None = None,
    ) -> None:
        self._windows = windows
        # Declared by the source, never inferred from the windows. Where the
        # windows happen to sit says how far somebody has written, not what is
        # complete: a file backfilled in pieces can hold two distant clusters
        # with an unrecorded year between them.
        self._covers = covers

    def mode_for(self, horizon: StrategyHorizon, now: datetime) -> EventRiskMode | None:
        """Most severe mode across the active windows, NORMAL when the instant
        is covered and none is active, and None when it falls outside what the
        calendar claims to cover."""
        if self._covers is None or not (self._covers[0] <= now <= self._covers[1]):
            return None
        mode = EventRiskMode.NORMAL
        for w in self._windows:
            if not w.active_at(now):
                continue
            action = w.actions.get(horizon, EventRiskMode.NORMAL)
            if _SEVERITY.index(action) > _SEVERITY.index(mode):
                mode = action
        return mode
