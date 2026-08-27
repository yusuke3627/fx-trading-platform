"""Scheduled event risk, scoped to the currencies an event moves.

Windows carry the currencies their event affects and a propagation policy
(ADR-017): a pair is gated only when the event reaches one of its legs —
an ECB decision alone must not stop USDJPY — while GLOBAL_CRITICAL events
(FOMC) hard-gate every pair regardless of direct legs, because volatility
propagates through synthetic crosses (GBPJPY ≒ GBPUSD × USDJPY).

Consecutive central-bank meetings still grade as one independent risk state:
overlapping windows answer with the most severe active mode, so a pair whose
legs span two adjacent decisions sees no calm gap between them. Thresholds
and windows live in YAML configuration, not in the spec text.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from trading.domain.instrument import InstrumentSpec
from trading.domain.money import Currency
from trading.domain.risk import EventRiskMode
from trading.strategy.base import StrategyHorizon

_SEVERITY = [EventRiskMode.NORMAL, EventRiskMode.REDUCED, EventRiskMode.HALT]


class EventPropagationPolicy(StrEnum):
    """イベントの影響がどのペアへ届くか（設計書 §14.1A）。"""

    DIRECT_LEGS = "DIRECT_LEGS"
    GLOBAL_CRITICAL = "GLOBAL_CRITICAL"
    # 依存グラフからの sensitivity 導出は次段階の scaffold。実装されるまで
    # は GLOBAL_CRITICAL と同じく全ペアに届く（保守側）。
    DEPENDENCY_GRAPH = "DEPENDENCY_GRAPH"


class EventRiskWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    first_event_at: datetime
    last_event_at: datetime
    pre_hours: int
    post_hours: int
    actions: dict[StrategyHorizon, EventRiskMode] = Field(default_factory=dict)
    # 空集合は「scope 未指定」で全ペアに適用（fail-close の従来互換）。
    affected_currencies: frozenset[Currency] = frozenset()
    propagation: EventPropagationPolicy = EventPropagationPolicy.DIRECT_LEGS

    def active_at(self, now: datetime) -> bool:
        start = self.first_event_at - timedelta(hours=self.pre_hours)
        end = self.last_event_at + timedelta(hours=self.post_hours)
        return start <= now <= end

    def applies_to(self, spec: InstrumentSpec) -> bool:
        if self.propagation is not EventPropagationPolicy.DIRECT_LEGS:
            return True
        if not self.affected_currencies:
            return True
        legs = {spec.base_currency, spec.quote_currency}
        return bool(self.affected_currencies & legs)


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
        """Most severe mode across ALL active windows regardless of scope,
        NORMAL when the instant is covered and none is active, and None when
        it falls outside what the calendar claims to cover."""
        return self._grade(self._windows, horizon, now)

    def mode_for_instrument(
        self, spec: InstrumentSpec, horizon: StrategyHorizon, now: datetime
    ) -> EventRiskMode | None:
        """mode_for を、このペアの leg に届く window だけに絞ったもの。

        `affected ∩ {base, quote}` が空の DIRECT_LEGS window はペアを
        止めない（ECB だけで USDJPY を止めない）。coverage の意味論は
        mode_for と同じ: 絞り込みは window の適用可否であって、暦が語れる
        期間を狭めない。"""
        return self._grade(
            [w for w in self._windows if w.applies_to(spec)], horizon, now
        )

    def _grade(
        self, windows: list[EventRiskWindow], horizon: StrategyHorizon, now: datetime
    ) -> EventRiskMode | None:
        if self._covers is None or not (self._covers[0] <= now <= self._covers[1]):
            return None
        mode = EventRiskMode.NORMAL
        for w in windows:
            if not w.active_at(now):
                continue
            if w.propagation is not EventPropagationPolicy.DIRECT_LEGS:
                # GLOBAL_CRITICAL（と scaffold の DEPENDENCY_GRAPH）は
                # horizon 別設定に依らず hard gate（設計書 §14.1A の
                # 「全ペアの new / risk-increasing entry を止める」）。
                return EventRiskMode.HALT
            action = w.actions.get(horizon, EventRiskMode.NORMAL)
            if _SEVERITY.index(action) > _SEVERITY.index(mode):
                mode = action
        return mode
