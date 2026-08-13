"""FX trading sessions (UTC windows)."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from enum import StrEnum


class Session(StrEnum):
    TOKYO = "TOKYO"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"


# (start_hour, end_hour) in UTC; approximate standard windows.
SESSION_WINDOWS_UTC: dict[Session, tuple[int, int]] = {
    Session.TOKYO: (0, 9),
    Session.LONDON: (7, 16),
    Session.NEW_YORK: (12, 21),
}


def sessions_at(ts: datetime) -> frozenset[Session]:
    utc = ts.astimezone(timezone.utc)
    active = {
        s for s, (start, end) in SESSION_WINDOWS_UTC.items() if start <= utc.hour < end
    }
    return frozenset(active)


def session_start(session: Session, ts: datetime) -> datetime:
    """Start of the given session's window on/preceding `ts` (UTC)."""
    utc = ts.astimezone(timezone.utc)
    start_hour, _ = SESSION_WINDOWS_UTC[session]
    start = datetime.combine(utc.date(), time(hour=start_hour, tzinfo=timezone.utc))
    if utc < start:
        start -= timedelta(days=1)
    return start
