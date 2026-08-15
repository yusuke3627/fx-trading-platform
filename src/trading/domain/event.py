"""Point-in-time event model.

Every external observation is stored as an event with a full time model so a
backtest can only see what was known at replay time:

    visible  <=>  known_at <= replay_clock.now()
"""
from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def ensure_json_native(value: object) -> None:
    """Reject anything json.dumps would silently coerce (tuples to lists,
    non-str keys to str, NaN/Infinity to invalid JSON): serializable is not
    enough, the JSONB round-trip must preserve types exactly.

    Runs at model construction AND again at the persistence boundary —
    frozen=True does not deep-freeze nested containers, so a payload can be
    mutated after validation.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("payload floats must be finite (no NaN/Infinity)")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                # ValueError, not TypeError: pydantic only wraps ValueError
                # into a ValidationError at the model boundary.
                raise ValueError(  # noqa: TRY004
                    f"payload dict keys must be str, got {type(key).__name__}"
                )
            ensure_json_native(item)
        return
    if isinstance(value, list):
        for item in value:
            ensure_json_native(item)
        return
    raise ValueError(
        f"payload contains non-JSON-native {type(value).__name__}; convert "
        "Decimals/datetimes/tuples at the collector boundary"
    )


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: UUID
    event_type: str
    source: str
    source_uri: str | None = None

    # JSON-native values only (str/int/float/bool/None/list/dict). Decimals,
    # datetimes etc. must be converted at the collector boundary: a JSONB
    # round-trip would otherwise silently change types between live and
    # replay, violating "same input -> same decision".
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _payload_is_json_native(cls, value: dict[str, Any]) -> dict[str, Any]:
        ensure_json_native(value)
        return value
    payload_hash: str | None = None
    raw_uri: str | None = None

    effective_at: datetime | None = None
    published_at: datetime | None = None
    retrieved_at: datetime
    known_at: datetime
    processed_at: datetime | None = None
    superseded_at: datetime | None = None


InterventionVerificationStatus = Literal[
    "RUMOR",
    "MARKET_SUSPECTED",
    "MEDIA_CONFIRMED",
    "OFFICIAL_ACTION_CONFIRMED",
    "OFFICIAL_AMOUNT_CONFIRMED",
]


class FXInterventionEvent(BaseModel):
    """FX intervention observation.

    Live estimates and later official MOF figures live in separate columns and
    are never overwritten into each other.
    """

    model_config = ConfigDict(frozen=True)

    event_id: UUID

    direction: Literal["JPY_BUY", "JPY_SELL"]
    verification_status: InterventionVerificationStatus

    reported_estimate_jpy: Decimal | None = None
    official_amount_jpy: Decimal | None = None

    effective_at: datetime
    known_at: datetime


class VerificationEvent(BaseModel):
    """A verification-status transition, recorded as a new fact.

    Status changes are never in-place updates; each transition is a new event
    with its own known_at.
    """

    model_config = ConfigDict(frozen=True)

    event_id: UUID
    subject_event_id: UUID
    from_status: str | None
    to_status: str
    known_at: datetime
