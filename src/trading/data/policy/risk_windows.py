"""Event-risk windows built from the central-bank meeting calendar.

`EventRiskCalendar` grades a horizon against the windows it is given; this is
where those windows come from. Configuration supplies how wide a window is and
what each horizon should do inside it, the meeting file supplies when.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from trading.data.policy.meetings import PolicyMeeting, load_coverage, load_meetings
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
    return EventRiskCalendar(
        central_bank_windows(load_meetings(), settings),
        (coverage.since, coverage.until) if coverage else None,
    )


def central_bank_windows(
    meetings: Sequence[PolicyMeeting], settings: EventRiskWindowSettings
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
    times = sorted(meeting.statement_published_at for meeting in meetings)
    if not times:
        return []

    pre = timedelta(hours=settings.pre_hours)
    post = timedelta(hours=settings.post_hours)
    clusters: list[list[datetime]] = [[times[0]]]
    for event_at in times[1:]:
        if event_at - pre <= clusters[-1][-1] + post:
            clusters[-1].append(event_at)
        else:
            clusters.append([event_at])

    actions = settings.actions()
    return [
        EventRiskWindow(
            name=CENTRAL_BANK_CLUSTER,
            first_event_at=cluster[0],
            last_event_at=cluster[-1],
            pre_hours=settings.pre_hours,
            post_hours=settings.post_hours,
            actions=actions,
        )
        for cluster in clusters
    ]
