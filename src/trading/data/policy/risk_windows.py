"""Event-risk windows built from the central-bank meeting calendar.

`EventRiskCalendar` grades a horizon against the windows it is given; this is
where those windows come from. Configuration supplies how wide a window is and
what each horizon should do inside it, the meeting file supplies when.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from trading.data.policy.meetings import (
    PolicyMeeting,
    ScheduledMeeting,
    load_coverage,
    load_meetings,
    load_schedule,
)
from trading.domain.money import Currency
from trading.risk.event_risk import (
    EventPropagationPolicy,
    EventRiskCalendar,
    EventRiskWindow,
)

if TYPE_CHECKING:
    from trading.config import AppConfig, EventRiskWindowSettings

# The one window kind configured today; the settings are keyed by it.
# Windows themselves are labelled per bank (ADR-017).
CENTRAL_BANK_CLUSTER = "dual_central_bank_cluster"

BANK_CURRENCIES: dict[str, Currency] = {
    "FED": Currency.USD,
    "BOJ": Currency.JPY,
    "BOE": Currency.GBP,
    "ECB": Currency.EUR,
}

# FOMC の政策決定は synthetic cross（GBPJPY ≒ GBPUSD × USDJPY）経由で
# 全ペアへ伝播するため GLOBAL_CRITICAL（設計書 §14.1A の initial safety
# decision）。他行は direct leg のみ。
BANK_PROPAGATION: dict[str, EventPropagationPolicy] = {
    "FED": EventPropagationPolicy.GLOBAL_CRITICAL,
    "BOJ": EventPropagationPolicy.DIRECT_LEGS,
    "BOE": EventPropagationPolicy.DIRECT_LEGS,
    "ECB": EventPropagationPolicy.DIRECT_LEGS,
}


def central_bank_calendar(config: AppConfig) -> EventRiskCalendar | None:
    """The calendar for the configured cluster window, or None when there is
    no such window configured.

    None is not "no event is near" — it is "no schedule is known", and the
    caller falls back to the configured default mode. Returning an empty
    calendar instead would claim every instant is quiet, which is the opposite
    of what an absent schedule tells you. Both the replay engine and the live
    runner go through here so the two cannot disagree about that.

    The coverage the file declares travels with the windows. A file that
    claims nothing produces a calendar that answers nothing, which keeps an
    unstated span from reading as a recorded one.
    """
    settings = config.event_risk.get(CENTRAL_BANK_CLUSTER)
    if settings is None:
        return None
    coverage = load_coverage()
    return EventRiskCalendar(
        central_bank_windows(load_meetings(), load_schedule(), settings),
        (coverage.since, coverage.until) if coverage else None,
    )


def central_bank_windows(
    meetings: Sequence[PolicyMeeting],
    schedule: Sequence[ScheduledMeeting],
    settings: EventRiskWindowSettings,
) -> list[EventRiskWindow]:
    """One window per bank per cluster of that bank's meetings.

    Consecutive central-bank decisions are one risk state rather than the sum
    of two (see risk/event_risk.py). What counts as consecutive is not a
    configured number, and overlap answers it without introducing one — but
    the clustering is per bank (ADR-017): a Fed and a BOJ decision two days
    apart stay two windows whose spans overlap, and mode_for_instrument
    grades the most severe active one. A pair whose legs span both decisions
    sees no calm gap between them, while a pair holding only one of the legs
    is gated only by its own bank's window — merging across banks would make
    a BOE meeting halt USDJPY.

    Publication instants are what the windows hang off — a decision is risk
    from the moment it can move the market, not from the calendar date it is
    filed under.

    The windows are not look-ahead. A scheduled meeting is announced months in
    advance, so a replay reacting to one 48 hours early is using the calendar
    everyone already had. An UNSCHEDULED meeting is the opposite: nobody could
    position for it beforehand, and recording one here would let a replay brace
    for a decision that arrived without warning. The file holds only regular
    meetings today; an emergency one needs its pre-window suppressed rather
    than simply being added.
    """
    # The announced interval is what the market positioned against, so it is
    # the only thing the window hangs off — before AND after the results come
    # in. The actual publication minute never reshapes the window in either
    # direction: these windows are not gated by query time, so stretching one
    # to a late actual publication would apply the stretch to a replay of the
    # hours before the statement landed, on knowledge from later. A statement
    # outside its recorded bounds means the bounds were curated wrong, which
    # is a data correction, not something to infer here. Each meeting enters
    # the clustering as one indivisible span, so the publication uncertainty
    # widens its window rather than shifting it — and can never split it, no
    # matter how the span compares to pre + post.
    unscheduled = {(m.bank, m.decision_date): m for m in meetings}
    spans_by_bank: dict[str, list[tuple[datetime, datetime]]] = {}
    for entry in schedule:
        unscheduled.pop((entry.bank, entry.decision_date), None)
        spans_by_bank.setdefault(entry.bank, []).append(
            (entry.earliest_published_at, entry.latest_published_at)
        )
    # Backfilled meetings predate the schedule section; their one recorded
    # instant is all the file knows.
    for m in unscheduled.values():
        spans_by_bank.setdefault(m.bank, []).append(
            (m.statement_published_at, m.statement_published_at)
        )

    pre = timedelta(hours=settings.pre_hours)
    post = timedelta(hours=settings.post_hours)
    actions = settings.actions()
    windows: list[EventRiskWindow] = []
    for bank in sorted(spans_by_bank):
        spans = sorted(spans_by_bank[bank])
        clusters: list[tuple[datetime, datetime]] = [spans[0]]
        for start, end in spans[1:]:
            first, last = clusters[-1]
            if start - pre <= last + post:
                clusters[-1] = (first, max(last, end))
            else:
                clusters.append((start, end))
        windows += [
            EventRiskWindow(
                name=f"central_bank:{bank}",
                first_event_at=first,
                last_event_at=last,
                pre_hours=settings.pre_hours,
                post_hours=settings.post_hours,
                actions=actions,
                affected_currencies=frozenset({BANK_CURRENCIES[bank]}),
                propagation=BANK_PROPAGATION[bank],
            )
            for first, last in clusters
        ]
    windows.sort(key=lambda w: (w.first_event_at, w.name))
    return windows
