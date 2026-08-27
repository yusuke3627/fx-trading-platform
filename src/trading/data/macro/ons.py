"""ONS forward collector (CPI, labour market, GDP).

api.ons.gov.uk の time-series API は 2024-11-25 に廃止済み（実測）。代わりに
ONS ウェブサイトの `/timeseries/{cdid}/{dataset}/data` JSON エンドポイントを
使う。文書化された契約ではないため、依存するフィールドだけを検査し、raw
payload を全量アーカイブして再パースに備える（ADR-015）。

Forward collection only: 返るのは現在値（改定後）で、known_at は取得時刻。
"""
from __future__ import annotations

from datetime import datetime
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
from trading.data.macro.registry import (
    INDICATORS,
    UK_CPI_HEADLINE_YOY_NSA,
    UK_REAL_GDP_GROWTH_QOQ_SA,
    UK_UNEMPLOYMENT_RATE_SA,
)
from trading.domain.economic import EconomicObservation

SOURCE_ONS = "ONS"
BASE_URL = "https://www.ons.gov.uk"

# canonical series -> (トピックパス, CDID, dataset, 期間キー)。
# 失業率は LFS のローリング3ヶ月平均で、ONS 自身が行に振る月
# （"2026 MAY" = APR-JUN 平均）をそのまま observation_period にする。
SERIES: dict[str, tuple[str, str, str, str]] = {
    UK_CPI_HEADLINE_YOY_NSA: (
        "economy/inflationandpriceindices", "d7g7", "mm23", "months",
    ),
    UK_UNEMPLOYMENT_RATE_SA: (
        "employmentandlabourmarket/peoplenotinwork/unemployment",
        "mgsx", "lms", "months",
    ),
    UK_REAL_GDP_GROWTH_QOQ_SA: (
        "economy/grossdomesticproductgdp", "ihyq", "pn2", "quarters",
    ),
}


def _period(token: str, period_key: str) -> str:
    year, part = token.split()
    if period_key == "quarters":
        # "2026 Q2" -> "2026Q2"
        return f"{year}{part}"
    return f"{year}-{MONTH_BY_ABBREV[part.upper()]:02d}"


def _published_at(token: str | None) -> datetime | None:
    if not token:
        return None
    return datetime.fromisoformat(token)


class ONSCollector:
    def __init__(self, transport: Any, *, clock: Clock | None = None) -> None:
        self._transport = transport
        self._clock = clock or SystemClock()

    def collect(self, series_name: str, years: list[int]) -> CollectionBatch:
        topic, cdid, dataset, period_key = SERIES[series_name]
        url = f"{BASE_URL}/{topic}/timeseries/{cdid}/{dataset}/data"
        payload = self._transport.get_json(url, {})
        rows = payload.get(period_key)
        if not rows:
            raise ValueError(f"ONS returned no {period_key} rows for {cdid}/{dataset}")

        retrieved_at = self._clock.now()
        spec = INDICATORS[series_name]
        page_hash = payload_hash(payload)
        observations: list[EconomicObservation] = []
        for row in rows:
            if int(row["year"]) < min(years):
                continue
            if not row["value"]:
                continue
            observations.append(
                EconomicObservation(
                    observation_id=uuid4(),
                    series=series_name,
                    observation_period=_period(row["date"], period_key),
                    value=Decimal(row["value"].replace(",", "")),
                    unit=spec.unit,
                    source=SOURCE_ONS,
                    source_uri=url,
                    payload_hash=page_hash,
                    # updateDate は ONS がその行を最後に更新した時刻。PIT の
                    # 可視性は known_at（取得時刻）が持ち、これは出所情報。
                    published_at=_published_at(row.get("updateDate")),
                    retrieved_at=retrieved_at,
                    known_at=retrieved_at,
                )
            )
        return CollectionBatch(
            observations=tuple(observations),
            raw_events=(
                raw_event(
                    source=SOURCE_ONS,
                    source_uri=url,
                    payload=payload,
                    retrieved_at=retrieved_at,
                ),
            ),
        )
