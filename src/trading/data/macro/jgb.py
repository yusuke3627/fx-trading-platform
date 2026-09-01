"""MOF JGB 2Y yield collector（財務省「国債金利情報」CSV）.

2ファイル併読: 全期間（jgbcm_all.csv、S49.9.24〜、月次更新で約1ヶ月遅れ）と
当月分（jgbcm.csv）を毎ランともに取得し、基準日でマージする。初回実行が
そのままバックフィルになり、月替わりでどちらのファイルに載っていても
取りこぼさない。

known_at（ADR-022）: MOF の公表は基準日の翌営業日 09:30 頃（公式 FAQ
https://www.mof.go.jp/faq/jgbs/04hf.htm 、実測 Last-Modified も一致）。
祝日カレンダーを持たずに翌営業日を得るため、マージ済み系列の「次の基準日」
を公表日の代理にし、09:30「頃」への余裕として 15:00 JST に置く。USDJPY
日足の close は 17:00 ET（翌暦日 06〜07:00 JST）なので、翌営業日の
09:30〜24:00 のどこに置いても日足整列は同じで、この余裕に実質コストはない。

次の基準日がまだ現れていない最新行は emit しない（known_at が決められない）。
known_at が決定論的になるため、再実行・独立 DB の複数ホストで同一 vintage に
なり、冪等性は repository の vintage キー + 同値スキップに乗る。過去行の値が
後日変わっても同じ known_at を計算して ON CONFLICT で落ち、初出値が保持され
る — 改定の真の公表時刻は CSV から知り得ないので、これが PIT として保守的。
"""
from __future__ import annotations

import csv
import io
import re
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from trading.backtest.clock import Clock, SystemClock
from trading.data.macro.base import CollectionBatch, payload_hash, raw_event
from trading.data.macro.registry import INDICATORS, JP_JGB_2Y_YIELD
from trading.domain.economic import EconomicObservation

SOURCE_MOF = "MOF"
HISTORY_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
CURRENT_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"

DATE_HEADER = "基準日"
MATURITY_HEADER = "2年"

# 公表 09:30 頃への余裕（モジュール docstring 参照）。
PUBLICATION_TIME_JST = time(15, 0)
_JST = ZoneInfo("Asia/Tokyo")

# S49.9.24 / H1.1.9 / R8.8.31 — 元号1文字 + 年.月.日（ゼロ埋めなし）。
# 介入 CSV の「令和8年6月29日」形式とは別物なので専用に持つ。
_ERA_BASE = {"S": 1925, "H": 1988, "R": 2018}
_WAREKI = re.compile(r"^([A-Z])(\d+)\.(\d+)\.(\d+)$")


def parse_wareki_short(token: str) -> date:
    match = _WAREKI.match(token)
    if match is None:
        raise ValueError(f"not a short-form wareki date: {token!r}")
    era, year, month, day = match.groups()
    if era not in _ERA_BASE:
        raise ValueError(f"unknown era letter in {token!r}")
    return date(_ERA_BASE[era] + int(year), int(month), int(day))


def _parse_csv(text: str, url: str) -> dict[date, Decimal | None]:
    """基準日 -> 2年複利利回り（欠測 "-" は None）。

    タイトル行・注記行・空行は基準日形式に一致しないので落ちる。値が None の
    日も返すのは、その基準日が前日の known_at（次の基準日）の材料になるため。
    """
    rows = list(csv.reader(io.StringIO(text)))
    header_index = next(
        (i for i, row in enumerate(rows) if row and row[0].strip() == DATE_HEADER), None
    )
    if header_index is None:
        raise ValueError(f"no {DATE_HEADER!r} header row in {url}")
    header = [cell.strip() for cell in rows[header_index]]
    if MATURITY_HEADER not in header:
        raise ValueError(f"no {MATURITY_HEADER!r} column in {url}: {header}")
    column = header.index(MATURITY_HEADER)

    by_day: dict[date, Decimal | None] = {}
    for row in rows[header_index + 1 :]:
        if not row or not _WAREKI.match(row[0].strip()):
            continue
        day = parse_wareki_short(row[0].strip())
        if len(row) <= column:
            raise ValueError(f"row for {day} has no {MATURITY_HEADER!r} column in {url}")
        cell = row[column].strip()
        if cell == "-":
            by_day[day] = None
            continue
        try:
            by_day[day] = Decimal(cell)
        except InvalidOperation:
            raise ValueError(f"unparseable {MATURITY_HEADER!r} value {cell!r} for {day} in {url}") from None
    if not by_day:
        raise ValueError(f"no data rows in {url}")
    return by_day


class JGBYieldCollector:
    def __init__(self, transport: Any, *, clock: Clock | None = None) -> None:
        self._transport = transport
        self._clock = clock or SystemClock()

    def collect(self) -> CollectionBatch:
        raw_events = []
        # (値, 出所URL, payload_hash, retrieved_at)。全期間 → 当月分の順で
        # 上書きし、重複日はより新しい公表である当月分を採る。
        merged: dict[date, tuple[Decimal | None, str, str, datetime]] = {}
        for url in (HISTORY_URL, CURRENT_URL):
            text = self._transport.get_bytes(url).decode("shift_jis")
            # 時刻は取得後に打つ（boe.py と同じ理由: 取得完了前の known_at を
            # 名乗らない）。
            retrieved_at = self._clock.now()
            raw_events.append(
                raw_event(
                    source=SOURCE_MOF,
                    source_uri=url,
                    payload={"csv": text},
                    retrieved_at=retrieved_at,
                )
            )
            page_hash = payload_hash({"csv": text})
            for day, value in _parse_csv(text, url).items():
                merged[day] = (value, url, page_hash, retrieved_at)

        spec = INDICATORS[JP_JGB_2Y_YIELD]
        observations = []
        # pairwise が最新行（次の基準日なし）を自然に落とす。
        for day, next_day in pairwise(sorted(merged)):
            value, url, page_hash, retrieved_at = merged[day]
            if value is None:
                continue
            known_at = datetime.combine(next_day, PUBLICATION_TIME_JST, _JST).astimezone(UTC)
            observations.append(
                EconomicObservation(
                    observation_id=uuid4(),
                    series=JP_JGB_2Y_YIELD,
                    observation_period=day.isoformat(),
                    value=value,
                    unit=spec.unit,
                    source=SOURCE_MOF,
                    source_uri=url,
                    payload_hash=page_hash,
                    retrieved_at=retrieved_at,
                    known_at=known_at,
                )
            )
        return CollectionBatch(
            observations=tuple(observations), raw_events=tuple(raw_events)
        )
