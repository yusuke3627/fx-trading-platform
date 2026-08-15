"""Economic release observations (point-in-time).

A release value is never updated in place: the first print and every later
revision are separate observations of the same (series, observation_period),
each with its own known_at. The revision chain is the known_at ordering within
one (series, observation_period) — replay at time t sees exactly the vintages
with known_at <= t.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

# "2026-07" (monthly) or "2026Q2" (quarterly).
_PERIOD_PATTERN = re.compile(r"^\d{4}(-(0[1-9]|1[0-2])|Q[1-4])$")


class EconomicObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    observation_id: UUID
    # Canonical indicator name (registry constant), shared across sources so
    # ALFRED vintage history and forward collection land in one series.
    series: str
    observation_period: str
    value: Decimal
    unit: str

    source: str
    source_uri: str | None = None
    payload_hash: str | None = None

    # When the source says the value was published (None when the API does not
    # carry a publication timestamp).
    published_at: datetime | None = None
    # When WE fetched it.
    retrieved_at: datetime
    # PIT visibility: for forward collection this is retrieved_at; for ALFRED
    # vintage reconstruction it is the vintage date combined with the official
    # release time-of-day.
    known_at: datetime

    @field_validator("observation_period")
    @classmethod
    def _period_format(cls, value: str) -> str:
        if not _PERIOD_PATTERN.match(value):
            raise ValueError(
                f"observation_period {value!r} must be YYYY-MM or YYYYQn"
            )
        return value
