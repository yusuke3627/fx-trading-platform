"""Intervention risk inputs from PIT events.

Produces the event-derived inputs of intelligence.intervention's risk score
(days_since_intervention, verification_state). Price-derived inputs
(realized_volatility, rate_of_change, distance_from_intervention_zone) join
once real price history exists; the score's renormalization already handles
their absence.

Pure functions over already-visible events: the caller fetches with
known_before(..., t), so everything here is PIT-safe. No recent intervention
yields an empty dict — absent input, not zero risk.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from trading.domain.event import EventEnvelope
from trading.intelligence.intervention import verification_state_level

INPUTS_VERSION = "intervention_inputs_v1"

# An intervention stops contributing to the risk inputs after this many days.
RECENCY_WINDOW_DAYS = 90

# Recognition stage of each event kind on the existing verification ladder.
KIND_TO_STATUS = {
    "INTERVENTION_MARKET_SUSPECTED": "MARKET_SUSPECTED",
    "INTERVENTION_REPORTED": "MEDIA_CONFIRMED",
    "INTERVENTION_GOVERNMENT_CONFIRMED": "OFFICIAL_ACTION_CONFIRMED",
    "INTERVENTION_OFFICIAL_DAILY_AMOUNT": "OFFICIAL_AMOUNT_CONFIRMED",
    "INTERVENTION_OFFICIAL_MONTHLY_AMOUNT": "OFFICIAL_AMOUNT_CONFIRMED",
}


def _action_date(event: EventEnvelope) -> date | None:
    payload = event.payload
    if "action_date" in payload:
        return date.fromisoformat(payload["action_date"])
    # Monthly totals aggregate a window; a positive total proves intervention
    # happened no later than the window end. Zero totals prove nothing.
    if payload.get("total_100m_yen", 0) > 0 and "period_end" in payload:
        return date.fromisoformat(payload["period_end"])
    return None


def intervention_risk_inputs(
    events: Sequence[EventEnvelope], t: datetime
) -> dict[str, float]:
    """Event-derived risk inputs at time t, all in [0, 1].

    days_since_intervention decays linearly from 1 (today) to 0 over
    RECENCY_WINDOW_DAYS. verification_state is the highest recognition stage
    reached for the most recent intervention inside the window.
    """
    latest: date | None = None
    for event in events:
        action = _action_date(event)
        if action is not None and (latest is None or action > latest):
            latest = action
    if latest is None:
        return {}

    days = (t.date() - latest).days
    if days < 0 or days > RECENCY_WINDOW_DAYS:
        return {}

    status_rank = -1
    order = ("MARKET_SUSPECTED", "MEDIA_CONFIRMED",
             "OFFICIAL_ACTION_CONFIRMED", "OFFICIAL_AMOUNT_CONFIRMED")
    status: str | None = None
    for event in events:
        if _action_date(event) != latest:
            continue
        mapped = KIND_TO_STATUS.get(event.event_type)
        if mapped is not None and order.index(mapped) > status_rank:
            status_rank = order.index(mapped)
            status = mapped

    inputs = {"days_since_intervention": 1.0 - days / RECENCY_WINDOW_DAYS}
    if status is not None:
        inputs["verification_state"] = verification_state_level(status)
    return inputs
