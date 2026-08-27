"""ECB Data Portal forward collector (SDMX-JSON).

参照エリア U2 は「拡大に追従する euro area」なので、Eurostat の固定構成
コード（EA20/EA21）と違い加盟国の追加で系列が切り替わらない。Forward
collection only: ポータルは現在値を返し、known_at は取得時刻（ADR-015）。

観測が 1 件もない期間への応答は空ボディで返る（実測）。日次連続系列を
使う限り 1 年以上の窓が空になることはないため、空応答は JSON パース失敗
として大きく落ちるのが正しい。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.backtest.clock import Clock, SystemClock
from trading.data.macro.base import CollectionBatch, payload_hash, raw_event
from trading.data.macro.registry import (
    EA_DEPOSIT_FACILITY_RATE,
    EA_YIELD_CURVE_2Y,
    INDICATORS,
)
from trading.domain.economic import EconomicObservation

SOURCE_ECB = "ECB"
DATA_URL = "https://data-api.ecb.europa.eu/service/data"

# canonical series -> dataflow/series-key リソースパス（日次連続系列を使う）。
SERIES_KEYS: dict[str, str] = {
    EA_DEPOSIT_FACILITY_RATE: "FM/D.U2.EUR.4F.KR.DFR.LEV",
    # AAA ソブリンカーブの 2Y spot（G_N_A = AAA 格のみ、SV_C_YM = spot、
    # SR_2Y = 2 年）。営業日次なので頻度は B。
    EA_YIELD_CURVE_2Y: "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y",
}


class ECBCollector:
    def __init__(self, transport: Any, *, clock: Clock | None = None) -> None:
        self._transport = transport
        self._clock = clock or SystemClock()

    def collect(self, series_name: str, years: list[int]) -> CollectionBatch:
        resource = SERIES_KEYS[series_name]
        url = f"{DATA_URL}/{resource}"
        payload = self._transport.get_json(
            url, {"format": "jsondata", "startPeriod": f"{min(years)}-01-01"}
        )

        datasets = payload.get("dataSets") or []
        if not datasets:
            raise ValueError(f"ECB returned no dataSets for {resource}")
        series = datasets[0].get("series") or {}
        if len(series) != 1:
            raise ValueError(
                f"expected exactly one ECB series for {resource}, got {len(series)}"
            )
        periods = [
            value["id"]
            for value in payload["structure"]["dimensions"]["observation"][0]["values"]
        ]

        retrieved_at = self._clock.now()
        spec = INDICATORS[series_name]
        page_hash = payload_hash(payload)
        (series_data,) = series.values()
        observations: list[EconomicObservation] = []
        for index, observation in series_data["observations"].items():
            value = observation[0]
            if value is None:
                continue
            observations.append(
                EconomicObservation(
                    observation_id=uuid4(),
                    series=series_name,
                    # SDMX の期間 id は日次/月次がそのまま通り、四半期のみ
                    # "2026-Q2" -> "2026Q2" の正規化が要る。
                    observation_period=periods[int(index)].replace("-Q", "Q"),
                    value=Decimal(str(value)),
                    unit=spec.unit,
                    source=SOURCE_ECB,
                    source_uri=url,
                    payload_hash=page_hash,
                    retrieved_at=retrieved_at,
                    known_at=retrieved_at,
                )
            )
        return CollectionBatch(
            observations=tuple(observations),
            raw_events=(
                raw_event(
                    source=SOURCE_ECB,
                    source_uri=url,
                    payload=payload,
                    retrieved_at=retrieved_at,
                ),
            ),
        )
