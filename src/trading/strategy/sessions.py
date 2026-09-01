from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

SESSION_NAMES = frozenset({"tokyo", "london", "new_york"})


class SessionEntryPolicy(StrEnum):
    PREFERRED = "PREFERRED"
    ALLOWED = "ALLOWED"
    SHADOW_ONLY = "SHADOW_ONLY"
    DISABLED = "DISABLED"


class SessionProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    sessions: dict[str, SessionEntryPolicy]

    @model_validator(mode="before")
    @classmethod
    def _normalize_session_names(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        sessions = data.get("sessions") if "sessions" in data else data
        if not isinstance(sessions, dict):
            return data
        normalized = {str(name).lower(): policy for name, policy in sessions.items()}
        unknown = normalized.keys() - SESSION_NAMES
        if unknown:
            raise ValueError(f"unknown session names: {sorted(unknown)}")
        if "sessions" in data:
            return {**data, "sessions": normalized}
        return {"sessions": normalized}

    def entry_allowed(self, session: str) -> bool:
        return self.sessions.get(session.lower()) in {
            SessionEntryPolicy.PREFERRED,
            SessionEntryPolicy.ALLOWED,
        }
