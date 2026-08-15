"""Census forward collector (advance retail sales, MARTS).

Forward collection: known_at = retrieved_at, same rule as bls.py. The EITS
API answers one calendar year per request with an array-of-arrays payload
(first row is the header), so the raw archive wraps it in a dict to stay a
JSON object like every other archived response.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.backtest.clock import Clock, SystemClock
from trading.data.macro.base import CollectionBatch, payload_hash, raw_event
from trading.data.macro.registry import INDICATORS, US_RETAIL_SALES_ADVANCE_SA
from trading.domain.economic import EconomicObservation

SOURCE_CENSUS = "CENSUS"
MARTS_URL = "https://api.census.gov/data/timeseries/eits/marts"

# Retail trade and food services, total; SM = sales, monthly, $ millions.
CATEGORY_CODE = "44X72"
DATA_TYPE_CODE = "SM"


class CensusCollector:
    def __init__(
        self, transport: Any, api_key: str | None = None, *, clock: Clock | None = None
    ) -> None:
        self._transport = transport
        self._api_key = api_key
        self._clock = clock or SystemClock()

    def collect(self, years: list[int]) -> CollectionBatch:
        spec = INDICATORS[US_RETAIL_SALES_ADVANCE_SA]
        observations: list[EconomicObservation] = []
        raw_events = []
        for year in sorted(years):
            params = {
                "get": "cell_value",
                "for": "us:*",
                "category_code": CATEGORY_CODE,
                "data_type_code": DATA_TYPE_CODE,
                "seasonally_adj": "yes",
                "time": str(year),
            }
            if self._api_key:
                params["key"] = self._api_key
            rows = self._transport.get_json(MARTS_URL, params)
            if not isinstance(rows, list) or not rows:
                raise ValueError(f"Census MARTS response for {year} is not tabular: {rows!r}")

            header = rows[0]
            value_idx = header.index("cell_value")
            time_idx = header.index("time")

            retrieved_at = self._clock.now()
            payload = {"rows": rows}
            page_hash = payload_hash(payload)
            raw_events.append(
                raw_event(
                    source=SOURCE_CENSUS,
                    source_uri=f"{MARTS_URL}?time={year}",
                    payload=payload,
                    retrieved_at=retrieved_at,
                )
            )
            for row in rows[1:]:
                observations.append(
                    EconomicObservation(
                        observation_id=uuid4(),
                        series=US_RETAIL_SALES_ADVANCE_SA,
                        # EITS time values are already "YYYY-MM".
                        observation_period=row[time_idx],
                        value=Decimal(row[value_idx].replace(",", "")),
                        unit=spec.unit,
                        source=SOURCE_CENSUS,
                        source_uri=MARTS_URL,
                        payload_hash=page_hash,
                        retrieved_at=retrieved_at,
                        known_at=retrieved_at,
                    )
                )
        return CollectionBatch(
            observations=tuple(observations), raw_events=tuple(raw_events)
        )
