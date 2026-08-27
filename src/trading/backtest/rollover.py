"""Broker rollover boundary と swap snapshot の PIT timeline（ADR-016）。

rollover は broker server の日付変更で発生する。server の壁時計は
「NY より broker_server_ahead_of_ny_hours 時間先行」という NY クローズ
規約（ADR-014 と同じ定義）なので、server midnight は NY ローカルの
(24 - ahead) 時にあたり、DST は America/New_York の壁時計経由で追従する。
"""
from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trading.domain.swap import SwapSnapshot

_NY = ZoneInfo("America/New_York")


def swap_dataset_fingerprint(snapshots: Sequence[SwapSnapshot]) -> str:
    """replay が消費する swap snapshot 列の content fingerprint。

    carry に効く値と可視時点（known_at）だけを畳む。row id や取得時刻は
    含めない: 同じ内容を別 DB で再収集しても一致し、値か可視性が違えば
    一致しない — manifest の再現性メタデータ用。
    """
    canonical = [
        (
            s.symbol,
            s.known_at.astimezone(UTC).isoformat(),
            s.swap_mode,
            str(s.swap_long),
            str(s.swap_short),
            s.swap_rollover3days,
            *(
                str(value) if value is not None else None
                for value in (
                    s.swap_sunday,
                    s.swap_monday,
                    s.swap_tuesday,
                    s.swap_wednesday,
                    s.swap_thursday,
                    s.swap_friday,
                    s.swap_saturday,
                )
            ),
        )
        for s in sorted(snapshots, key=lambda s: (s.symbol, s.known_at))
    ]
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


def _boundary_time_of_day(server_ahead_of_ny_hours: float) -> time:
    minutes = round((24 - server_ahead_of_ny_hours) * 60) % (24 * 60)
    return time(minutes // 60, minutes % 60)


def next_rollover_boundary(after: datetime, server_ahead_of_ny_hours: float) -> datetime:
    """`after` より厳密に後の、次の server midnight（UTC instant）。"""
    tod = _boundary_time_of_day(server_ahead_of_ny_hours)
    local = after.astimezone(_NY)
    candidate_date = local.date()
    candidate = datetime.combine(candidate_date, tod, _NY).astimezone(UTC)
    while candidate <= after:
        candidate_date += timedelta(days=1)
        candidate = datetime.combine(candidate_date, tod, _NY).astimezone(UTC)
    return candidate


def ended_server_day(boundary: datetime) -> date:
    """boundary で終わった broker server 日付（swap 倍率の曜日キー）。

    server は NY より 0〜24h 先行するので、boundary 直前の server 日付は
    boundary 時点の NY ローカル日付と常に一致する。
    """
    return boundary.astimezone(_NY).date()


def server_midnight_label(boundary: datetime) -> datetime:
    """boundary の broker wall-clock ラベル（= server midnight、ADR-014 軸）。

    tick.time / opened_at はこのラベル軸にあるため、「rollover 時点で
    broker の帳簿に載っていたか」はこのラベルと比較する — known-time 軸の
    boundary と直接比較すると遅延受信 tick で判定がずれる。
    """
    return datetime.combine(
        ended_server_day(boundary) + timedelta(days=1), time(0), UTC
    )


class SwapTimeline:
    """1 シンボルの swap snapshot 列。known_at <= t の最新を返す。"""

    def __init__(self, snapshots: Sequence[SwapSnapshot], symbol: str) -> None:
        # 同一 known_at の tiebreak は id: 入力順（DB の返却順）に依存する
        # 未定義の latest を作らず、storage の ORDER BY known_at, id と
        # 同じ行を latest に選ぶ。
        rows = sorted(
            (s for s in snapshots if s.symbol == symbol),
            key=lambda s: (s.known_at, s.snapshot_id),
        )
        self._rows = rows
        self._known = [s.known_at for s in rows]

    def __bool__(self) -> bool:
        return bool(self._rows)

    def latest_known_before(self, t: datetime) -> SwapSnapshot | None:
        index = bisect_right(self._known, t)
        return self._rows[index - 1] if index else None
