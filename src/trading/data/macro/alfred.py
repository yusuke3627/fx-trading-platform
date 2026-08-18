"""ALFRED vintage collector (St. Louis Fed).

The one free source that answers "what value was visible on a given date":
each observation row carries the real-time window in which the value was
current, so first prints and every revision come back as separate vintages.

known_at precision: ALFRED vintages are dated to the day. The vintage date is
combined with the indicator's official release time-of-day (registry) so an
08:30 ET print becomes visible at 12:30Z/13:30Z, not at midnight — a midnight
known_at would leak the value ~13 hours early into a replay.

Vintage windows: the observations endpoint rejects real-time periods spanning
more than 2,000 vintage dates (observed live with DGS2: 5,090). Collection
therefore first lists the series' vintage dates and queries observations one
<=2,000-vintage window at a time. A value whose real-time window crosses a
window boundary reappears in the next window with a clamped realtime_start;
the repository's unchanged-value rule drops that duplicate, so the chain
stays clean without special-casing here.
"""
from __future__ import annotations

from datetime import date, timedelta
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
    US_TREASURY_2Y_YIELD,
    US_UNEMPLOYMENT_RATE_SA,
    period_from_date,
)
from trading.domain.economic import EconomicObservation

SOURCE_ALFRED = "ALFRED"
OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
VINTAGE_DATES_URL = "https://api.stlouisfed.org/fred/series/vintagedates"

SERIES_IDS: dict[str, str] = {
    US_CPI_HEADLINE_SA: "CPIAUCSL",
    US_CPI_CORE_SA: "CPILFESL",
    US_NONFARM_PAYROLLS_SA: "PAYEMS",
    US_UNEMPLOYMENT_RATE_SA: "UNRATE",
    US_REAL_GDP_GROWTH_SAAR: "A191RL1Q225SBEA",
    US_RETAIL_SALES_ADVANCE_SA: "RSAFS",
    # Same official Treasury H.15 data as home.treasury.gov, redistributed by
    # FRED with real-time vintages — which is what makes it PIT-usable.
    US_TREASURY_2Y_YIELD: "DGS2",
}

REALTIME_ALL_END = "9999-12-31"

# API maximum. Full vintage history of a 1947-origin monthly series exceeds
# one page, so collection follows the offset/count pagination contract.
PAGE_LIMIT = 100_000

# Documented observations-endpoint limit on vintage dates per real-time
# period (error 400 beyond it), and the vintagedates endpoint's page limit.
MAX_VINTAGES_PER_WINDOW = 2_000
VINTAGE_DATES_PAGE_LIMIT = 10_000


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
        for realtime_start, realtime_end in self._vintage_windows(series_id):
            offset = 0
            while True:
                params = {
                    "series_id": series_id,
                    "api_key": self._api_key,
                    "file_type": "json",
                    "realtime_start": realtime_start,
                    "realtime_end": realtime_end,
                    "limit": str(PAGE_LIMIT),
                    "offset": str(offset),
                }
                if observation_start is not None:
                    params["observation_start"] = observation_start.isoformat()
                page = self._transport.get_json(OBSERVATIONS_URL, params)
                if "observations" not in page:
                    raise ValueError(
                        f"ALFRED response for {series_id} has no observations: {page}"
                    )

                retrieved_at = self._clock.now()
                raw_events.append(
                    raw_event(
                        source=SOURCE_ALFRED,
                        source_uri=(
                            f"{OBSERVATIONS_URL}?series_id={series_id}"
                            f"&realtime_start={realtime_start}&offset={offset}"
                        ),
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

    def _vintage_windows(self, series_id: str) -> list[tuple[str, str]]:
        """Real-time windows covering the full vintage history, each holding
        at most MAX_VINTAGES_PER_WINDOW vintage dates."""
        dates: list[str] = []
        offset = 0
        while True:
            page = self._transport.get_json(
                VINTAGE_DATES_URL,
                {
                    "series_id": series_id,
                    "api_key": self._api_key,
                    "file_type": "json",
                    "limit": str(VINTAGE_DATES_PAGE_LIMIT),
                    "offset": str(offset),
                },
            )
            if "vintage_dates" not in page:
                raise ValueError(
                    f"ALFRED vintagedates response for {series_id} is malformed: {page}"
                )
            dates.extend(page["vintage_dates"])
            offset += len(page["vintage_dates"])
            if offset >= int(page["count"]) or not page["vintage_dates"]:
                break

        windows: list[tuple[str, str]] = []
        for start in range(0, len(dates), MAX_VINTAGES_PER_WINDOW):
            chunk = dates[start : start + MAX_VINTAGES_PER_WINDOW]
            next_start = start + MAX_VINTAGES_PER_WINDOW
            if next_start >= len(dates):
                end = REALTIME_ALL_END
            else:
                # Up to the day before the next window's first vintage, so
                # every vintage date lands in exactly one window.
                end = (date.fromisoformat(dates[next_start]) - timedelta(days=1)).isoformat()
            windows.append((chunk[0], end))
        return windows
