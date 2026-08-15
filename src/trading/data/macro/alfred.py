"""ALFRED vintage collector (St. Louis Fed).

The one free source that answers "what value was visible on a given date":
each observation row carries the real-time window in which the value was
current, so first prints and every revision come back as separate vintages.

known_at precision: ALFRED vintages are dated to the day. The vintage date is
combined with the indicator's official release time-of-day (registry) so an
08:30 ET print becomes visible at 12:30Z/13:30Z, not at midnight — a midnight
known_at would leak the value ~13 hours early into a replay.
"""
from __future__ import annotations

from datetime import date
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
    US_REAL_GDP_GROWTH_SAAR,
    US_RETAIL_SALES_ADVANCE_SA,
    US_UNEMPLOYMENT_RATE_SA,
    period_from_date,
)
from trading.domain.economic import EconomicObservation

SOURCE_ALFRED = "ALFRED"
OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

SERIES_IDS: dict[str, str] = {
    US_CPI_HEADLINE_SA: "CPIAUCSL",
    US_CPI_CORE_SA: "CPILFESL",
    US_NONFARM_PAYROLLS_SA: "PAYEMS",
    US_UNEMPLOYMENT_RATE_SA: "UNRATE",
    US_REAL_GDP_GROWTH_SAAR: "A191RL1Q225SBEA",
    US_RETAIL_SALES_ADVANCE_SA: "RSAFS",
}

# FRED's documented "all vintages" real-time window.
REALTIME_ALL_START = "1776-07-04"
REALTIME_ALL_END = "9999-12-31"

# API maximum. Full vintage history of a 1947-origin monthly series exceeds
# one page, so collection follows the offset/count pagination contract.
PAGE_LIMIT = 100_000


class AlfredCollector:
    def __init__(self, transport: Any, api_key: str, *, clock: Clock | None = None) -> None:
        self._transport = transport
        self._api_key = api_key
        self._clock = clock or SystemClock()

    def collect(self, series: str, *, observation_start: date | None = None) -> CollectionBatch:
        spec = INDICATORS[series]
        series_id = SERIES_IDS[series]

        observations: list[EconomicObservation] = []
        raw_events = []
        offset = 0
        while True:
            params = {
                "series_id": series_id,
                "api_key": self._api_key,
                "file_type": "json",
                "realtime_start": REALTIME_ALL_START,
                "realtime_end": REALTIME_ALL_END,
                "limit": str(PAGE_LIMIT),
                "offset": str(offset),
            }
            if observation_start is not None:
                params["observation_start"] = observation_start.isoformat()
            page = self._transport.get_json(OBSERVATIONS_URL, params)
            if "observations" not in page:
                raise ValueError(f"ALFRED response for {series_id} has no observations: {page}")

            retrieved_at = self._clock.now()
            raw_events.append(
                raw_event(
                    source=SOURCE_ALFRED,
                    source_uri=f"{OBSERVATIONS_URL}?series_id={series_id}&offset={offset}",
                    payload=page,
                    retrieved_at=retrieved_at,
                )
            )
            page_hash = payload_hash(page)
            for row in page["observations"]:
                # "." is FRED's explicit missing-value marker.
                if row["value"] == ".":
                    continue
                vintage_date = date.fromisoformat(row["realtime_start"])
                observations.append(
                    EconomicObservation(
                        observation_id=uuid4(),
                        series=series,
                        observation_period=period_from_date(
                            date.fromisoformat(row["date"]), spec.frequency
                        ),
                        value=Decimal(row["value"]),
                        unit=spec.unit,
                        source=SOURCE_ALFRED,
                        source_uri=OBSERVATIONS_URL,
                        payload_hash=page_hash,
                        retrieved_at=retrieved_at,
                        known_at=spec.release_instant(vintage_date),
                    )
                )

            offset += len(page["observations"])
            if offset >= int(page["count"]) or not page["observations"]:
                break

        return CollectionBatch(
            observations=tuple(observations), raw_events=tuple(raw_events)
        )
