"""BOE forward collector (Bank Rate via the IADB CSV interface).

Forward collection only: the IADB serves current values with no vintage
axis, so known_at is when WE fetched them and the series stays
PIT_UNVERIFIED (ADR-015). The endpoint answers HTML with HTTP 200 when the
query is malformed, so the parser rejects anything that is not the expected
CSV header instead of storing zero observations.
"""
from __future__ import annotations

import csv
import io
import urllib.parse
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.backtest.clock import Clock, SystemClock
from trading.data.macro.base import (
    MONTH_BY_ABBREV,
    CollectionBatch,
    payload_hash,
    raw_event,
)
from trading.data.macro.registry import INDICATORS, UK_BANK_RATE
from trading.domain.economic import EconomicObservation

SOURCE_BOE = "BOE"
IADB_URL = "https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp"

SERIES_CODES: dict[str, str] = {UK_BANK_RATE: "IUDBEDR"}

_ABBREV_BY_MONTH = {number: abbrev.title() for abbrev, number in MONTH_BY_ABBREV.items()}


def _iadb_date(value: date) -> str:
    return f"{value.day:02d}/{_ABBREV_BY_MONTH[value.month]}/{value.year}"


def _row_date(token: str) -> date:
    day, month_abbrev, year = token.split()
    return date(int(year), MONTH_BY_ABBREV[month_abbrev.upper()], int(day))


class BOECollector:
    def __init__(self, transport: Any, *, clock: Clock | None = None) -> None:
        self._transport = transport
        self._clock = clock or SystemClock()

    def collect(self, years: list[int]) -> CollectionBatch:
        series_code = SERIES_CODES[UK_BANK_RATE]
        params = {
            "csv.x": "yes",
            "Datefrom": _iadb_date(date(min(years), 1, 1)),
            "Dateto": _iadb_date(self._clock.now().date()),
            "SeriesCodes": series_code,
            "CSVF": "TN",
            "UsingCodes": "Y",
            "VPD": "Y",
            "VFD": "N",
        }
        url = f"{IADB_URL}?{urllib.parse.urlencode(params)}"
        text = self._transport.get_bytes(url).decode("utf-8-sig")
        # 時刻は取得後に打つ。取得前だと known_at が実際の取得完了より
        # 早くなり、その間に置いた replay clock から、まだ受け取って
        # いなかった値が見えてしまう。
        retrieved_at = self._clock.now()

        rows = list(csv.reader(io.StringIO(text)))
        if not rows or rows[0] != ["DATE", series_code]:
            raise ValueError(f"unexpected IADB response header: {text[:120]!r}")
        if len(rows) < 2:
            raise ValueError(f"IADB returned a header with no observations: {url}")

        spec = INDICATORS[UK_BANK_RATE]
        page_hash = payload_hash({"csv": text})
        observations = tuple(
            EconomicObservation(
                observation_id=uuid4(),
                series=UK_BANK_RATE,
                observation_period=_row_date(row[0]).isoformat(),
                value=Decimal(row[1]),
                unit=spec.unit,
                source=SOURCE_BOE,
                source_uri=url,
                payload_hash=page_hash,
                retrieved_at=retrieved_at,
                known_at=retrieved_at,
            )
            for row in rows[1:]
            if row
        )
        return CollectionBatch(
            observations=observations,
            raw_events=(
                raw_event(
                    source=SOURCE_BOE,
                    source_uri=url,
                    payload={"csv": text},
                    retrieved_at=retrieved_at,
                ),
            ),
        )
