"""Eurostat forward collector (statistics API, JSON-stat 2.0).

euro area の固定構成 geo コードは拡大のたびに切り替わり（2026-01 に
EA20 -> EA21）、dataset ごとに移行のタイミングも違う — GDP は両方で応答、
失業率は EA21 のみ、HICP は EA20 のみ（実測 2026-08-26）。そのため毎回
全候補 geo を並べて要求し、期間ごとに「値が実在する最新構成」を採用する。

dissemination API は最新値しか返さない: vintage 軸が存在しないため
known_at は取得時刻で、系列は PIT_UNVERIFIED（ADR-015）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.backtest.clock import Clock, SystemClock
from trading.data.macro.base import CollectionBatch, payload_hash, raw_event
from trading.data.macro.registry import (
    EA_HICP_HEADLINE_YOY_NSA,
    EA_REAL_GDP_GROWTH_QOQ_SCA,
    EA_UNEMPLOYMENT_RATE_SA,
    INDICATORS,
)
from trading.domain.economic import EconomicObservation

SOURCE_EUROSTAT = "EUROSTAT"
BASE_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"

# 新しい構成が先頭。EA は「変動構成」の集計で、最後の受け皿。
GEO_CANDIDATES = ("EA21", "EA20", "EA")

# canonical series -> (dataset, 次元フィルタ)。geo と time 以外の次元は
# ここで一意に絞り切る（絞れていない場合はデコーダが落とす）。
SERIES: dict[str, tuple[str, dict[str, str]]] = {
    EA_HICP_HEADLINE_YOY_NSA: (
        "prc_hicp_manr", {"coicop": "CP00", "unit": "RCH_A"},
    ),
    EA_UNEMPLOYMENT_RATE_SA: (
        "une_rt_m", {"s_adj": "SA", "age": "TOTAL", "sex": "T", "unit": "PC_ACT"},
    ),
    EA_REAL_GDP_GROWTH_QOQ_SCA: (
        "namq_10_gdp", {"unit": "CLV_PCH_PRE", "s_adj": "SCA", "na_item": "B1GQ"},
    ),
}


def _best_geo_values(payload: dict[str, Any]) -> dict[str, Decimal]:
    """期間 -> 値。同一期間に複数 geo の値があれば最新構成を採る。"""
    ids: list[str] = payload["id"]
    sizes: list[int] = payload["size"]
    ordered: dict[str, list[str]] = {}
    for dim in ids:
        index: dict[str, int] = payload["dimension"][dim]["category"]["index"]
        codes: list[str] = [""] * len(index)
        for code, position in index.items():
            codes[position] = code
        ordered[dim] = codes
    for dim, size in zip(ids, sizes):
        if dim not in ("geo", "time") and size > 1:
            raise ValueError(
                f"Eurostat dimension {dim!r} is not pinned down; add it to the filters"
            )

    preference = {code: rank for rank, code in enumerate(GEO_CANDIDATES)}
    best: dict[str, tuple[int, Decimal]] = {}
    for flat_key, value in payload["value"].items():
        remainder = int(flat_key)
        coordinates: dict[str, str] = {}
        for dim, size in zip(reversed(ids), reversed(sizes)):
            coordinates[dim] = ordered[dim][remainder % size]
            remainder //= size
        period = coordinates["time"]
        rank = preference[coordinates["geo"]]
        if period not in best or rank < best[period][0]:
            best[period] = (rank, Decimal(str(value)))
    return {period: value for period, (_, value) in best.items()}


class EurostatCollector:
    def __init__(self, transport: Any, *, clock: Clock | None = None) -> None:
        self._transport = transport
        self._clock = clock or SystemClock()

    def collect(self, series_name: str, years: list[int]) -> CollectionBatch:
        dataset, filters = SERIES[series_name]
        url = f"{BASE_URL}/{dataset}"
        params: dict[str, str | list[str]] = {
            "format": "JSON",
            "lang": "EN",
            "sinceTimePeriod": str(min(years)),
            "geo": list(GEO_CANDIDATES),
            **filters,
        }
        payload = self._transport.get_json(url, params)
        values = _best_geo_values(payload)
        if not values:
            # フィルタ切れ（geo コード改定・dataset 構造変更）と更新停止は
            # どちらもここに落ちる。黙って 0 件保存にしない。
            raise ValueError(
                f"Eurostat returned no observations for {dataset} since {min(years)}"
            )

        retrieved_at = self._clock.now()
        spec = INDICATORS[series_name]
        page_hash = payload_hash(payload)
        observations = tuple(
            EconomicObservation(
                observation_id=uuid4(),
                series=series_name,
                # 月次 "2026-07" はそのまま、四半期のみ "2026-Q2" -> "2026Q2"。
                observation_period=period.replace("-Q", "Q"),
                value=value,
                unit=spec.unit,
                source=SOURCE_EUROSTAT,
                source_uri=url,
                payload_hash=page_hash,
                retrieved_at=retrieved_at,
                known_at=retrieved_at,
            )
            for period, value in values.items()
        )
        return CollectionBatch(
            observations=observations,
            raw_events=(
                raw_event(
                    source=SOURCE_EUROSTAT,
                    source_uri=url,
                    payload=payload,
                    retrieved_at=retrieved_at,
                ),
            ),
        )
