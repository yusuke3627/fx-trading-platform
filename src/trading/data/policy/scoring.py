"""Mechanical policy-shift scoring (research note 2026-08-15).

Interpretation scale: -2 strongly dovish … +2 strongly hawkish. Rule weights
are summed and clipped to [-2, +2]; the algorithm is versioned so a future
re-tuning creates new events instead of silently rewriting old backtests.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from trading.backtest.clock import Clock
from trading.data.policy.meetings import PolicyMeeting
from trading.domain.event import EventEnvelope

SCORING_VERSION = "policy_shift_v1"

RATE_HIKE_SCORE = 2.0
RATE_CUT_SCORE = -2.0
HAWKISH_DISSENT_SCORE = 0.5
DOVISH_DISSENT_SCORE = -0.5
INFLATION_FORECAST_SCORE = 0.5
FUTURE_HIKE_LANGUAGE_SCORE = 0.5

SCORE_MIN = -2.0
SCORE_MAX = 2.0

EVENT_TYPES = {"BOJ": "BOJ_POLICY_SHIFT_SCORE", "FED": "FED_POLICY_SHIFT_SCORE"}


def score_meeting(meeting: PolicyMeeting) -> float:
    score = 0.0
    if meeting.rate_change_bp > 0:
        score += RATE_HIKE_SCORE
    elif meeting.rate_change_bp < 0:
        score += RATE_CUT_SCORE
    score += HAWKISH_DISSENT_SCORE * meeting.hawkish_dissents
    score += DOVISH_DISSENT_SCORE * meeting.dovish_dissents
    score += INFLATION_FORECAST_SCORE * meeting.inflation_forecast_change
    if meeting.explicit_future_hike_language:
        score += FUTURE_HIKE_LANGUAGE_SCORE
    return max(SCORE_MIN, min(SCORE_MAX, score))


def event_from_meeting(meeting: PolicyMeeting, clock: Clock) -> EventEnvelope:
    # Deterministic id: re-ingesting the same meeting under the same scoring
    # version is a no-op at the store; a new scoring version creates new
    # events instead of overwriting history.
    event_id = uuid5(
        NAMESPACE_URL,
        f"policy-meeting:{meeting.bank}:{meeting.decision_date}:{SCORING_VERSION}",
    )
    return EventEnvelope(
        event_id=event_id,
        event_type=EVENT_TYPES[meeting.bank],
        source=f"{meeting.bank}_OFFICIAL",
        source_uri=meeting.source_uri,
        payload={
            "score": score_meeting(meeting),
            "scoring_version": SCORING_VERSION,
            "decision_date": meeting.decision_date.isoformat(),
            "rate_change_bp": meeting.rate_change_bp,
            "hawkish_dissents": meeting.hawkish_dissents,
            "dovish_dissents": meeting.dovish_dissents,
            "inflation_forecast_change": meeting.inflation_forecast_change,
            "explicit_future_hike_language": meeting.explicit_future_hike_language,
            "verified": meeting.verified,
        },
        effective_at=meeting.statement_published_at,
        published_at=meeting.statement_published_at,
        retrieved_at=clock.now(),
        known_at=meeting.statement_published_at,
    )
