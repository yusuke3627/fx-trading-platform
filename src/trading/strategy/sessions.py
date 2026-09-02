from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from trading.indicators.session import sessions_at

SESSION_NAMES = frozenset({"tokyo", "london", "new_york"})


class SessionEntryPolicy(StrEnum):
    PREFERRED = "PREFERRED"
    ALLOWED = "ALLOWED"
    SHADOW_ONLY = "SHADOW_ONLY"
    DISABLED = "DISABLED"


# 重なった session の policy を 1 つに畳むときの緩さの順。
_PERMISSIVENESS: dict[SessionEntryPolicy, int] = {
    SessionEntryPolicy.DISABLED: 0,
    SessionEntryPolicy.SHADOW_ONLY: 1,
    SessionEntryPolicy.ALLOWED: 2,
    SessionEntryPolicy.PREFERRED: 3,
}


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

    def policy_at(self, ts: datetime) -> SessionEntryPolicy | None:
        """`ts` に開いている session のうち最も緩い policy。

        session が重なる時間帯は緩い側を採る。開いている session が無い、
        または profile に載っていない session しか開いていなければ None。
        """
        names = (session.value.lower() for session in sessions_at(ts))
        policies = [self.sessions[name] for name in names if name in self.sessions]
        if not policies:
            return None
        return max(policies, key=_PERMISSIVENESS.__getitem__)

    def permits_entry(self, ts: datetime, *, live: bool) -> bool:
        policy = self.policy_at(ts)
        if policy is SessionEntryPolicy.SHADOW_ONLY:
            return not live
        return policy in {SessionEntryPolicy.PREFERRED, SessionEntryPolicy.ALLOWED}
