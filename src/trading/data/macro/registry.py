"""Canonical indicator registry.

One canonical series name per indicator, shared by every source: ALFRED
vintage reconstruction and forward collection from the statistical agencies
must land in the same series or the revision chain falls apart.

Per-source identifiers (BLS series ids, BEA table names, ...) live in the
collector modules; this registry holds only what is source-independent.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

US_CPI_HEADLINE_SA = "us_cpi_headline_sa"
US_CPI_CORE_SA = "us_cpi_core_sa"
US_NONFARM_PAYROLLS_SA = "us_nonfarm_payrolls_sa"
US_UNEMPLOYMENT_RATE_SA = "us_unemployment_rate_sa"
US_REAL_GDP_GROWTH_SAAR = "us_real_gdp_growth_saar"
US_RETAIL_SALES_ADVANCE_SA = "us_retail_sales_advance_sa"


class IndicatorSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    series: str
    unit: str
    frequency: Literal["monthly", "quarterly"]
    # Official release time-of-day in the agency's timezone, used to place a
    # vintage date on the intraday timeline (all six initial indicators are
    # 08:30 ET releases).
    release_time: time
    release_timezone: str

    def release_instant(self, vintage_date: date) -> datetime:
        """The UTC instant a vintage published on `vintage_date` became known.

        The timezone conversion goes through the agency's local zone so DST is
        respected (08:30 ET is 12:30Z in summer, 13:30Z in winter). Using the
        official time-of-day instead of midnight matters for look-ahead: a
        midnight known_at would show the value ~13 hours before the release.
        """
        local = datetime.combine(
            vintage_date, self.release_time, ZoneInfo(self.release_timezone)
        )
        return local.astimezone(UTC)


_ET = "America/New_York"
_0830 = time(8, 30)

INDICATORS: dict[str, IndicatorSpec] = {
    spec.series: spec
    for spec in (
        IndicatorSpec(
            series=US_CPI_HEADLINE_SA,
            unit="index",
            frequency="monthly",
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_CPI_CORE_SA,
            unit="index",
            frequency="monthly",
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_NONFARM_PAYROLLS_SA,
            unit="thousands_of_persons",
            frequency="monthly",
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_UNEMPLOYMENT_RATE_SA,
            unit="percent",
            frequency="monthly",
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_REAL_GDP_GROWTH_SAAR,
            unit="percent",
            frequency="quarterly",
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_RETAIL_SALES_ADVANCE_SA,
            unit="millions_of_dollars",
            frequency="monthly",
            release_time=_0830,
            release_timezone=_ET,
        ),
    )
}


def period_from_date(observation_date: date, frequency: str) -> str:
    """Reference-period string for an observation date (FRED-style dates mark
    the period start: 2026-07-01 is July 2026, 2026-04-01 is 2026Q2)."""
    if frequency == "monthly":
        return f"{observation_date.year}-{observation_date.month:02d}"
    quarter = (observation_date.month - 1) // 3 + 1
    return f"{observation_date.year}Q{quarter}"
