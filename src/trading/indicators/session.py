"""IANA timezone で定義した FX 市場セッション。

`ts` は実時刻を表す aware datetime とする。fixed-offset の server 時刻表現は
`astimezone` で同じ実時刻へ正規化されるが、broker clock の壁時計を UTC と称した
datetime を渡すと、その offset 分だけ誤判定する。
"""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class Session(StrEnum):
    TOKYO = "TOKYO"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"


# ローカル市場時間の窓（timezone 名、開始 hour、終了 hour の半開区間）
SESSION_WINDOWS_LOCAL: dict[Session, tuple[str, int, int]] = {
    Session.TOKYO: ("Asia/Tokyo", 9, 18),
    Session.LONDON: ("Europe/London", 8, 17),
    Session.NEW_YORK: ("America/New_York", 8, 17),
}

_SESSION_TIMEZONES: dict[Session, ZoneInfo] = {
    session: ZoneInfo(timezone_name)
    for session, (timezone_name, _, _) in SESSION_WINDOWS_LOCAL.items()
}


def sessions_at(ts: datetime) -> frozenset[Session]:
    active = {
        session
        for session, (_, start, end) in SESSION_WINDOWS_LOCAL.items()
        if start <= ts.astimezone(_SESSION_TIMEZONES[session]).hour < end
    }
    return frozenset(active)


def session_start(session: Session, ts: datetime) -> datetime:
    """Start of the given session's window on/preceding `ts` (UTC)."""
    timezone = _SESSION_TIMEZONES[session]
    local = ts.astimezone(timezone)
    _, start_hour, _ = SESSION_WINDOWS_LOCAL[session]
    start = datetime.combine(local.date(), time(hour=start_hour), tzinfo=timezone)
    if local < start:
        start = datetime.combine(
            local.date() - timedelta(days=1),
            time(hour=start_hour),
            tzinfo=timezone,
        )
    return start.astimezone(UTC)
