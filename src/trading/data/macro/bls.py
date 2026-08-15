"""BLS forward collector (CPI, employment).

Forward collection only: the BLS time-series API returns current (possibly
revised) values, so known_at is when WE fetched them — never backdated.
Historical first prints are ALFRED's job (alfred.py); the two sources share
canonical series names so their vintages form one chain.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.backtest.clock import Clock, SystemClock
from trading.data.macro.base import CollectionBatch, payload_hash, raw_event
from trading.data.macro.registry import (
    INDICATORS,
    US_CPI_CORE_SA,
    US_CPI_HEADLINE_SA,
    US_NONFARM_PAYROLLS_SA,
    US_UNEMPLOYMENT_RATE_SA,
)
from trading.domain.economic import EconomicObservation

SOURCE_BLS = "BLS"
TIMESERIES_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

SERIES_IDS: dict[str, str] = {
    US_CPI_HEADLINE_SA: "CUSR0000SA0",
    US_CPI_CORE_SA: "CUSR0000SA0L1E",
    US_NONFARM_PAYROLLS_SA: "CES0000000001",
    US_UNEMPLOYMENT_RATE_SA: "LNS14000000",
}
_SERIES_BY_ID = {v: k for k, v in SERIES_IDS.items()}


class BLSCollector:
    def __init__(
        self, transport: Any, api_key: str | None = None, *, clock: Clock | None = None
    ) -> None:
        self._transport = transport
        self._api_key = api_key
        self._clock = clock or SystemClock()

    def collect(self, series_names: list[str], years: list[int]) -> CollectionBatch:
        body: dict[str, Any] = {
            "seriesid": [SERIES_IDS[name] for name in series_names],
            "startyear": str(min(years)),
            "endyear": str(max(years)),
        }
        if self._api_key:
            body["registrationkey"] = self._api_key
        payload = self._transport.post_json(TIMESERIES_URL, body)
        if payload.get("status") != "REQUEST_SUCCEEDED":
            raise ValueError(f"BLS request failed: {payload.get('message')}")

        retrieved_at = self._clock.now()
        page_hash = payload_hash(payload)
        observations: list[EconomicObservation] = []
        for series_block in payload["Results"]["series"]:
            name = _SERIES_BY_ID[series_block["seriesID"]]
            spec = INDICATORS[name]
            for row in series_block["data"]:
                # M01..M12 are months; M13 is the annual average, which is not
                # a release the market trades.
                if not row["period"].startswith("M") or row["period"] == "M13":
                    continue
                month = int(row["period"][1:])
                observations.append(
                    EconomicObservation(
                        observation_id=uuid4(),
                        series=name,
                        observation_period=f"{row['year']}-{month:02d}",
                        value=Decimal(row["value"].replace(",", "")),
                        unit=spec.unit,
                        source=SOURCE_BLS,
                        source_uri=TIMESERIES_URL,
                        payload_hash=page_hash,
                        retrieved_at=retrieved_at,
                        known_at=retrieved_at,
                    )
                )
        return CollectionBatch(
            observations=tuple(observations),
            raw_events=(
                raw_event(
                    source=SOURCE_BLS,
                    source_uri=TIMESERIES_URL,
                    payload=payload,
                    retrieved_at=retrieved_at,
                ),
            ),
        )
