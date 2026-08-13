"""LLM boundary.

An LLM may only turn text (news, speeches) into structured events. It never
receives broker credentials or execution tools, and nothing in this module may
import execution/OMS code: LLM output terminates at structured events.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class StructuredExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)


class StructuredEventExtractor(Protocol):
    enabled: bool

    def extract(
        self, text: str, *, source: str, known_at: datetime
    ) -> list[StructuredExtraction]: ...


class DisabledExtractor:
    """Initial state: LLM off, zero cost, no dependency."""

    enabled = False

    def extract(
        self, text: str, *, source: str, known_at: datetime
    ) -> list[StructuredExtraction]:
        return []
