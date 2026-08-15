"""BEA forward collector (real GDP growth).

Forward collection: known_at = retrieved_at, same rule as bls.py. The NIPA
percent-change table serves the Advance/Second/Third estimates in place, so
re-collecting after each estimate lands the successive vintages as separate
rows via the (series, period, known_at) uniqueness key.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.backtest.clock import Clock, SystemClock
from trading.data.macro.base import CollectionBatch, payload_hash, raw_event
from trading.data.macro.registry import INDICATORS, US_REAL_GDP_GROWTH_SAAR
from trading.domain.economic import EconomicObservation

SOURCE_BEA = "BEA"
DATA_URL = "https://apps.bea.gov/api/data/"

# T10101 is NIPA table 1.1.1 (percent change from preceding period, SAAR);
# line 1 is GDP. Not T10111: that is table 1.1.11, percent change from quarter
# one year ago — a different series than ALFRED's A191RL1Q225SBEA vintages
# (confirmed live 2026-08-16: T10101 matches the latest vintages exactly).
NIPA_TABLE = "T10101"
GDP_LINE_NUMBER = "1"


class BEACollector:
    def __init__(self, transport: Any, api_key: str, *, clock: Clock | None = None) -> None:
        self._transport = transport
        self._api_key = api_key
        self._clock = clock or SystemClock()

    def collect(self, years: list[int]) -> CollectionBatch:
        spec = INDICATORS[US_REAL_GDP_GROWTH_SAAR]
        params = {
            "UserID": self._api_key,
            "method": "GetData",
            "DataSetName": "NIPA",
            "TableName": NIPA_TABLE,
            "Frequency": "Q",
            "Year": ",".join(str(y) for y in sorted(years)),
            "ResultFormat": "JSON",
        }
        payload = self._transport.get_json(DATA_URL, params)
        results = payload.get("BEAAPI", {}).get("Results", {})
        if "Error" in results or "Data" not in results:
            raise ValueError(f"BEA request failed: {results.get('Error', results)}")

        retrieved_at = self._clock.now()
        page_hash = payload_hash(payload)
        observations: list[EconomicObservation] = []
        for row in results["Data"]:
            if row.get("LineNumber") != GDP_LINE_NUMBER:
                continue
            observations.append(
                EconomicObservation(
                    observation_id=uuid4(),
                    series=US_REAL_GDP_GROWTH_SAAR,
                    # BEA's TimePeriod ("2026Q1") already matches the canonical
                    # period format.
                    observation_period=row["TimePeriod"],
                    value=Decimal(row["DataValue"].replace(",", "")),
                    unit=spec.unit,
                    source=SOURCE_BEA,
                    source_uri=DATA_URL,
                    payload_hash=page_hash,
                    retrieved_at=retrieved_at,
                    known_at=retrieved_at,
                )
            )
        return CollectionBatch(
            observations=tuple(observations),
            raw_events=(
                raw_event(
                    source=SOURCE_BEA,
                    source_uri=DATA_URL,
                    payload=payload,
                    retrieved_at=retrieved_at,
                ),
            ),
        )
