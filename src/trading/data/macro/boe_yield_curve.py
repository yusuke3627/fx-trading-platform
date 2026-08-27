"""BOE OIS spot curve forward collector（ADR-020）。

BOE はイールドカーブ推計を zip 入りの Excel でしか配布しない（統計ページに
CSV の配布経路は無い — 実測 2026-08-27）。採るのは spot カーブの 2 年点だけ
で、これは会合パスの proxy として `US_TREASURY_2Y_YIELD` と年限を揃える
ため（通貨間の減算は同じ年限どうしでしか意味を持たない）。

配布は 2 ファイルに割れている。履歴アーカイブは前月末で終わり、当月ぶんは
別 zip に載る（実測: アーカイブは 2026-07-31 まで、当月ファイルが 2026-08-03
以降）。窓を跨いで欠測を作らないよう両方を読み、日付で重ねる。

Forward collection only: 配布ファイルに vintage 軸は無いので known_at は
取得時刻で、系列は PIT_UNVERIFIED（ADR-015）。
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import openpyxl

from trading.backtest.clock import Clock, SystemClock
from trading.data.macro.base import CollectionBatch, raw_event
from trading.data.macro.registry import INDICATORS, UK_OIS_2Y
from trading.domain.economic import EconomicObservation

SOURCE_BOE = "BOE"
YIELD_CURVE_BASE = (
    "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves"
)
ARCHIVE_URL = f"{YIELD_CURVE_BASE}/oisddata.zip"
LATEST_URL = f"{YIELD_CURVE_BASE}/latest-yield-curve-data.zip"

SPOT_CURVE_SHEET = "4. spot curve"
MATURITY_HEADER = "years:"
TARGET_MATURITY_YEARS = 2

CURRENT_MONTH_MEMBER = "OIS daily data current month.xlsx"
# アーカイブの分割は "OIS daily data_2016 to 2024.xlsx" / "_2025 to present.xlsx"。
_ERA = re.compile(r"_(\d{4}) to (\d{4}|present)\.xlsx$", re.IGNORECASE)


class BOEYieldCurveCollector:
    def __init__(self, transport: Any, *, clock: Clock | None = None) -> None:
        self._transport = transport
        self._clock = clock or SystemClock()

    def collect(self, years: list[int]) -> CollectionBatch:
        now = self._clock.now()
        spec = INDICATORS[UK_OIS_2Y]

        curve: dict[date, tuple[Decimal, str, str]] = {}
        raw_events = []
        # アーカイブが先、当月ファイルが後。両方に載る日付は新しい配布を採る。
        for url in (ARCHIVE_URL, LATEST_URL):
            raw = self._transport.get_bytes(url)
            fetched = _read_curve(raw, url, years)
            event = raw_event(
                source=SOURCE_BOE,
                source_uri=url,
                # workbook 自体は archive しない（アーカイブ zip だけで 11MB
                # あり、日次で events へ積むと桁が合わない）。配布ファイルの
                # digest を残して出所を辿れるようにする。
                payload={
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "maturity_years": TARGET_MATURITY_YEARS,
                    "curve": {
                        day.isoformat(): str(value)
                        for day, value in sorted(fetched.items())
                    },
                },
                retrieved_at=now,
            )
            raw_events.append(event)
            for day, value in fetched.items():
                curve[day] = (value, url, event.payload_hash)

        if not curve:
            # シート構成が変わって 1 行も取れないのと、配信が止まっているのは
            # 区別が付かない。0 件を「データ無し」として通すと、欠測に気づく
            # のは正規化が窓を満たせなくなった後になる。
            raise ValueError(f"the BOE OIS spot curve yielded no observations for {years}")

        observations = tuple(
            EconomicObservation(
                observation_id=uuid4(),
                series=UK_OIS_2Y,
                observation_period=day.isoformat(),
                value=value,
                unit=spec.unit,
                source=SOURCE_BOE,
                source_uri=source_uri,
                payload_hash=page_hash,
                retrieved_at=now,
                known_at=now,
            )
            for day, (value, source_uri, page_hash) in sorted(curve.items())
        )
        return CollectionBatch(observations=observations, raw_events=tuple(raw_events))


def _read_curve(raw: bytes, url: str, years: list[int]) -> dict[date, Decimal]:
    archive = zipfile.ZipFile(io.BytesIO(raw))
    members = [name for name in archive.namelist() if _covers(name, years)]
    if not members:
        raise ValueError(
            f"no OIS workbook covering {years} in {url}: {archive.namelist()}"
        )
    wanted = set(years)
    curve: dict[date, Decimal] = {}
    for name in sorted(members):
        curve.update(
            (day, value)
            # member は年代単位でしか切れない（"_2016 to 2024" で 9 年ぶん）。
            # 行の側でも要求年に絞らないと、呼び出し側が指定した期間を超えた
            # 履歴を raw event と DB へ毎回積むことになる。
            for day, value in _read_workbook(
                archive.read(name), f"{url}#{name}"
            ).items()
            if day.year in wanted
        )
    return curve


def _covers(name: str, years: list[int]) -> bool:
    """OIS の member か、かつ要求年に重なるか。

    当月 zip には GLC（名目・実質・インフレ）カーブも同梱されるので、
    名前で OIS だけに絞る。
    """
    if name == CURRENT_MONTH_MEMBER:
        return True
    era = _ERA.search(name)
    if era is None:
        return False
    start = int(era.group(1))
    end = None if era.group(2).lower() == "present" else int(era.group(2))
    return any(year >= start and (end is None or year <= end) for year in years)


def _read_workbook(data: bytes, source: str) -> dict[date, Decimal]:
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = workbook[SPOT_CURVE_SHEET]
        column: int | None = None
        curve: dict[date, Decimal] = {}
        for row in sheet.iter_rows(values_only=True):
            if column is None:
                column = _maturity_column(row, source)
                continue
            # 見出しの下には日付でない行（休日の空行、集計の残骸）が挟まる。
            if not row or not isinstance(row[0], datetime):
                continue
            value = row[column] if column < len(row) else None
            if value is None:
                continue
            curve[row[0].date()] = Decimal(str(value))
        if column is None:
            raise ValueError(f"no {MATURITY_HEADER!r} header row in {source}")
        return curve
    finally:
        workbook.close()


def _maturity_column(row: tuple[Any, ...], source: str) -> int | None:
    if not row or row[0] != MATURITY_HEADER:
        return None
    for index, maturity in enumerate(row):
        if maturity == TARGET_MATURITY_YEARS:
            return index
    raise ValueError(
        f"the OIS spot curve in {source} has no {TARGET_MATURITY_YEARS}-year column"
    )
