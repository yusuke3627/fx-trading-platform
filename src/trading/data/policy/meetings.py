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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_MEETINGS_PATH = Path("config/policy_meetings.yaml")


class PolicyMeeting(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

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


class MeetingCoverage(BaseModel):
    """The span the meeting file claims to be complete over.

    Not the range of the meetings it lists: that only says how far somebody
    has written. A file backfilled in pieces can hold January and December
    with nothing in between, and the gap is unrecorded rather than quiet —
    telling the two apart is what this exists for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    since: datetime
    until: datetime

    @field_validator("since", "until")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("coverage bounds must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _ordered(self) -> MeetingCoverage:
        if self.until < self.since:
            raise ValueError("coverage until must not precede since")
        return self


def load_coverage(path: Path | str = DEFAULT_MEETINGS_PATH) -> MeetingCoverage | None:
    """What the file says it covers, or None when it makes no claim.

    None means the schedule is unknown everywhere, so risk falls back to its
    configured default rather than reading an unstated span as quiet.
    """
    raw = yaml.safe_load(Path(path).read_text()) or {}
    covers = raw.get("covers")
    return MeetingCoverage.model_validate(covers) if covers else None


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


class ScheduledMeeting(BaseModel):
    """A meeting announced on the official calendar whose results are not out.

    Schedule entries feed the risk windows only and never reach scoring. A
    placeholder scored as "hold, no dissents" would be persisted by the daily
    collector under the meeting's deterministic event id, and the correction
    transcribed later would land on ON CONFLICT DO NOTHING — the wrong event
    would be permanent. Keeping the schedule structurally separate makes that
    path impossible instead of guarded.

    Publication is a bounded interval, not an instant: BOJ announces "right
    after the meeting" with no fixed clock time. The window opens off the
    early bound and closes off the late one, so the uncertainty widens the
    window instead of shifting it. A bank with a fixed time (FED) records the
    same instant twice. extra is forbidden so results transcribed onto a
    schedule entry by mistake fail loudly instead of silently never scoring.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bank: Literal["BOJ", "FED"]
    decision_date: date
    earliest_published_at: datetime
    latest_published_at: datetime
    source_uri: str

    @field_validator("earliest_published_at", "latest_published_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("publication bounds must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _ordered(self) -> ScheduledMeeting:
        if self.latest_published_at < self.earliest_published_at:
            raise ValueError("latest_published_at must not precede earliest_published_at")
        return self


def load_schedule(path: Path | str = DEFAULT_MEETINGS_PATH) -> list[ScheduledMeeting]:
    """Announced-only meetings. A meeting present here and in meetings: is a
    move that forgot its second half, so it is rejected rather than shadowed."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    schedule = [ScheduledMeeting.model_validate(entry) for entry in raw.get("schedule", [])]
    seen = {(m.bank, m.decision_date) for m in load_meetings(path)}
    for entry in schedule:
        key = (entry.bank, entry.decision_date)
        if key in seen:
            raise ValueError(f"duplicate meeting entry: {key}")
        seen.add(key)
    return schedule
