"""Curated market-recognition timeline of interventions.

MOF statistics say what happened; they cannot say when the market learned it.
That timeline (suspected -> reported -> government confirmed) is curated yaml
with a source URI per entry, same approach as policy meetings. Timestamps not
yet verified against contemporaneous coverage carry a conservative later
bound and verified: false.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from trading.backtest.clock import Clock
from trading.domain.event import EventEnvelope

DEFAULT_EPISODES_PATH = Path("config/intervention_episodes.yaml")

SOURCE_CURATED = "CURATED_TIMELINE"

RecognitionKind = Literal["MARKET_SUSPECTED", "REPORTED", "GOVERNMENT_CONFIRMED"]

EVENT_TYPE_PREFIX = "INTERVENTION_"


class InterventionRecognition(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: RecognitionKind
    action_date: date
    # When the market reached this recognition stage (upper bound if not
    # verified against the actual first report).
    known_at: datetime
    direction: Literal["JPY_BUY", "JPY_SELL"]
    # Estimates circulating at the time, never the later official figure.
    reported_estimate_100m_yen: int | None = None
    verified: bool
    source_uri: str
    note: str = ""

    @field_validator("known_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("known_at must be timezone-aware")
        return value


def load_episodes(path: Path | str = DEFAULT_EPISODES_PATH) -> list[InterventionRecognition]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = [
        InterventionRecognition.model_validate(entry)
        for entry in raw.get("recognitions", [])
    ]
    seen: set[tuple[str, date]] = set()
    for entry in entries:
        key = (entry.kind, entry.action_date)
        if key in seen:
            raise ValueError(f"duplicate recognition entry: {key}")
        seen.add(key)
    return entries


def event_from_recognition(
    recognition: InterventionRecognition, clock: Clock
) -> EventEnvelope:
    payload: dict = {
        "action_date": recognition.action_date.isoformat(),
        "direction": recognition.direction,
        "verified": recognition.verified,
    }
    if recognition.reported_estimate_100m_yen is not None:
        payload["reported_estimate_100m_yen"] = recognition.reported_estimate_100m_yen
    if recognition.note:
        payload["note"] = recognition.note
    return EventEnvelope(
        event_id=uuid5(
            NAMESPACE_URL,
            f"intervention-recognition:{recognition.kind}:{recognition.action_date}",
        ),
        event_type=f"{EVENT_TYPE_PREFIX}{recognition.kind}",
        source=SOURCE_CURATED,
        source_uri=recognition.source_uri,
        payload=payload,
        retrieved_at=clock.now(),
        known_at=recognition.known_at,
    )
