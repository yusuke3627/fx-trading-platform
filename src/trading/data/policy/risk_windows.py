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
from trading.risk.event_risk import EventRiskCalendar, EventRiskWindow

if TYPE_CHECKING:
    from trading.config import AppConfig, EventRiskWindowSettings

# The one window kind configured today. Named here because the settings are
# keyed by it and the windows are labelled with it.
CENTRAL_BANK_CLUSTER = "dual_central_bank_cluster"


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
    # Announced-only meetings open windows just like transcribed ones — the
    # market braces for the date, not for the outcome. They stay out of
    # scoring, which is why they live in a separate section of the file.
    return EventRiskCalendar(
        central_bank_windows([*load_meetings(), *load_schedule()], settings),
        (coverage.since, coverage.until) if coverage else None,
    )


def central_bank_windows(
    meetings: Sequence[PolicyMeeting | ScheduledMeeting],
    settings: EventRiskWindowSettings,
) -> list[EventRiskWindow]:
    """One window per cluster of meetings whose risk periods run together.

    Consecutive central-bank decisions are one risk state rather than the sum
    of two (see risk/event_risk.py). What counts as consecutive is not a
    configured number, and overlap answers it without introducing one: a Fed
    and a BOJ decision two days apart sit inside each other's window and become
    a single one, while decisions months apart stay separate.

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
    # A transcribed meeting published at one known instant; a scheduled one
    # only bounds it. Each meeting enters the clustering as one indivisible
    # span, so the publication uncertainty widens its window rather than
    # shifting it — and can never split it, no matter how the span compares
    # to pre + post.
    spans = sorted(
        (meeting.statement_published_at, meeting.statement_published_at)
        if isinstance(meeting, PolicyMeeting)
        else (meeting.earliest_published_at, meeting.latest_published_at)
        for meeting in meetings
    )
    if not spans:
        return []

    pre = timedelta(hours=settings.pre_hours)
    post = timedelta(hours=settings.post_hours)
    clusters: list[tuple[datetime, datetime]] = [spans[0]]
    for start, end in spans[1:]:
        first, last = clusters[-1]
        if start - pre <= last + post:
            clusters[-1] = (first, max(last, end))
        else:
            clusters.append((start, end))

    actions = settings.actions()
    return [
        EventRiskWindow(
            name=CENTRAL_BANK_CLUSTER,
            first_event_at=first,
            last_event_at=last,
            pre_hours=settings.pre_hours,
            post_hours=settings.post_hours,
            actions=actions,
        )
        for first, last in clusters
    ]
