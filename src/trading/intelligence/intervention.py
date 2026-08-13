"""FX intervention: verification state machine and reproducible risk feature.

Verification transitions are forward-only and each transition is recorded as
a new VerificationEvent (never an in-place update). The risk feature uses only
reproducible inputs — no hindsight manual labels — and its dictionary/weights
are versioned.
"""
from __future__ import annotations

from datetime import datetime
from typing import Mapping
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from trading.domain.event import VerificationEvent

VERIFICATION_ORDER: tuple[str, ...] = (
    "RUMOR",
    "MARKET_SUSPECTED",
    "MEDIA_CONFIRMED",
    "OFFICIAL_ACTION_CONFIRMED",
    "OFFICIAL_AMOUNT_CONFIRMED",
)


class VerificationRegressionError(ValueError):
    pass


def advance_verification(
    subject_event_id: UUID,
    current_status: str | None,
    new_status: str,
    known_at: datetime,
) -> VerificationEvent:
    """Create the next verification event. Skipping stages forward is allowed
    (e.g. straight to OFFICIAL_ACTION_CONFIRMED); moving backward is not."""
    if new_status not in VERIFICATION_ORDER:
        raise ValueError(f"unknown verification status: {new_status!r}")
    if current_status is not None:
        if current_status not in VERIFICATION_ORDER:
            raise ValueError(f"unknown current status: {current_status!r}")
        if VERIFICATION_ORDER.index(new_status) <= VERIFICATION_ORDER.index(current_status):
            raise VerificationRegressionError(
                f"verification cannot regress: {current_status} -> {new_status}"
            )
    return VerificationEvent(
        event_id=uuid4(),
        subject_event_id=subject_event_id,
        from_status=current_status,
        to_status=new_status,
        known_at=known_at,
    )


class InterventionRiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    weights: dict[str, float] = Field(default_factory=dict)


# Reproducible inputs; all normalized to [0, 1] by their producers.
RISK_INPUTS: tuple[str, ...] = (
    "official_wording_score",
    "realized_volatility",
    "distance_from_intervention_zone",
    "days_since_intervention",
    "verification_state",
    "rate_of_change",
)


def intervention_risk_score(
    inputs: Mapping[str, float],
    config: InterventionRiskConfig,
) -> float:
    """Weighted sum of available inputs, renormalized over present weights and
    clipped to [0, 1]. Missing inputs reduce coverage instead of counting as
    zero risk."""
    total_weight = 0.0
    score = 0.0
    for name in RISK_INPUTS:
        weight = config.weights.get(name, 0.0)
        if weight <= 0 or name not in inputs:
            continue
        total_weight += weight
        score += weight * max(0.0, min(1.0, inputs[name]))
    if total_weight == 0:
        return 0.0
    return max(0.0, min(1.0, score / total_weight))


def verification_state_level(status: str | None) -> float:
    """Verification status as a [0, 1] feature input."""
    if status is None or status not in VERIFICATION_ORDER:
        return 0.0
    return (VERIFICATION_ORDER.index(status) + 1) / len(VERIFICATION_ORDER)
