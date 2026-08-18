"""Curated central-bank meeting facts.

The scoring inputs (rate action, vote split, forecast direction) are small
structured facts — eight BOJ meetings a year — kept as a versioned yaml with a
source URI per meeting. Parsing statements automatically is deliberately out
of scope: the research note requires reproducible mechanical inputs, and a
human transcribing the official statement is the most reliable parser at this
volume.

known_at comes from statement_published_at. FOMC statements have a fixed time
(14:00 ET); BOJ does not publish a fixed time, so BOJ entries record a
conservative later-bound until the actual publication minute is verified.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_MEETINGS_PATH = Path("config/policy_meetings.yaml")


class PolicyMeeting(BaseModel):
    model_config = ConfigDict(frozen=True)

    bank: Literal["BOJ", "FED"]
    decision_date: date
    statement_published_at: datetime

    # Basis points of the policy-rate change decided (0 for a hold).
    rate_change_bp: int = 0
    hawkish_dissents: int = Field(default=0, ge=0)
    dovish_dissents: int = Field(default=0, ge=0)
    # Direction of the inflation-outlook revision in the same publication
    # cycle: -1 downgrade, 0 unchanged/none, +1 upgrade.
    inflation_forecast_change: Literal[-1, 0, 1] = 0
    explicit_future_hike_language: bool = False

    # False until every field has been checked against the official
    # statement; unverified entries still score, but research runs can
    # exclude them.
    verified: bool
    source_uri: str

    @field_validator("statement_published_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("statement_published_at must be timezone-aware")
        return value


def load_meetings(path: Path | str = DEFAULT_MEETINGS_PATH) -> list[PolicyMeeting]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    meetings = [PolicyMeeting.model_validate(entry) for entry in raw.get("meetings", [])]
    seen: set[tuple[str, date]] = set()
    for meeting in meetings:
        key = (meeting.bank, meeting.decision_date)
        if key in seen:
            raise ValueError(f"duplicate meeting entry: {key}")
        seen.add(key)
    return meetings
