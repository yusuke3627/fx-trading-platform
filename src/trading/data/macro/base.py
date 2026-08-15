"""Shared collector plumbing: raw-response archive and payload hashing.

Every HTTP response a collector parses is also archived verbatim as an
EventEnvelope, so a parsing bug can be replayed against the original payload
instead of re-fetching data the source may have revised since.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from trading.domain.economic import EconomicObservation
from trading.domain.event import EventEnvelope

ECONOMIC_RELEASE_RAW = "ECONOMIC_RELEASE_RAW"


def payload_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def raw_event(
    *,
    source: str,
    source_uri: str,
    payload: dict[str, Any],
    retrieved_at: datetime,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=ECONOMIC_RELEASE_RAW,
        source=source,
        source_uri=source_uri,
        payload=payload,
        payload_hash=payload_hash(payload),
        retrieved_at=retrieved_at,
        known_at=retrieved_at,
    )


class CollectionBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    observations: tuple[EconomicObservation, ...]
    raw_events: tuple[EventEnvelope, ...]
