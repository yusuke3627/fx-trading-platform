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
# Policy proxy input, not a "Fed expectation": the 2Y yield mixes policy
# expectations with term/growth/inflation/risk premia (research note
# 2026-08-15), so the series is named for what it is.
US_TREASURY_2Y_YIELD = "us_treasury_2y_yield"

UK_BANK_RATE = "uk_bank_rate"
# CPI/HICP は指数でなく前年比を正本にする: 指数は基準改定で系列が切れる
# （HICP は 2026-01 の 2025=100 移行で全 geo の指数系列が 2025-12 終端 —
# 実測 2026-08-26）が、前年比は基準に依存しない（ADR-015）。
UK_CPI_HEADLINE_YOY_NSA = "uk_cpi_headline_yoy_nsa"
UK_UNEMPLOYMENT_RATE_SA = "uk_unemployment_rate_sa"
UK_REAL_GDP_GROWTH_QOQ_SA = "uk_real_gdp_growth_qoq_sa"
# 会合パスの proxy（ADR-020）。MPC Dated SONIA futures の代わりに BOE の
# OIS spot カーブから 2Y 点を採る。US_TREASURY_2Y_YIELD と同じ年限にして
# あるのは、通貨間の減算が同じ年限どうしでしか意味を持たないため。
UK_OIS_2Y = "uk_ois_2y"

# ample-reserves レジームの実効政策金利は預金ファシリティ金利（MRO ではない）。
EA_DEPOSIT_FACILITY_RATE = "ea_deposit_facility_rate"
EA_HICP_HEADLINE_YOY_NSA = "ea_hicp_headline_yoy_nsa"
EA_UNEMPLOYMENT_RATE_SA = "ea_unemployment_rate_sa"
EA_REAL_GDP_GROWTH_QOQ_SCA = "ea_real_gdp_growth_qoq_sca"
# ユーロ圏の会合パス proxy（ADR-020）。ECB は OIS カーブを公表しないため
# AAA ソブリンカーブの 2Y spot を使う。信用・流動性プレミアムが乗るぶん
# GBP 側の OIS より proxy として遠い。
EA_YIELD_CURVE_2Y = "ea_yield_curve_2y"

# 系列の「収集開始前の履歴」を真の vintage として復元できるか（ADR-015）。
# PIT_VERIFIED: vintage アーカイブ（ALFRED）が release 時点の known_at を裏付ける。
# PIT_UNVERIFIED: ソースが最新値しか返さないため、PIT が成立するのは自前の
# forward snapshot 以降のみ。バックフィルした履歴に release 時刻の known_at を
# 与えてはならず、strict OOS 評価は収集開始前の期間を除外する。
PIT_VERIFIED = "PIT_VERIFIED"
PIT_UNVERIFIED = "PIT_UNVERIFIED"


class IndicatorSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    series: str
    unit: str
    frequency: Literal["daily", "monthly", "quarterly"]
    pit_classification: Literal["PIT_VERIFIED", "PIT_UNVERIFIED"]
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
_LONDON = "Europe/London"
_BRUSSELS = "Europe/Brussels"
_0830 = time(8, 30)
# ONS の統計公表は 07:00 ロンドン時刻。
_0700 = time(7, 0)
# Eurostat のニュースリリースは 11:00 ブリュッセル時刻。
_1100 = time(11, 0)

INDICATORS: dict[str, IndicatorSpec] = {
    spec.series: spec
    for spec in (
        IndicatorSpec(
            series=US_CPI_HEADLINE_SA,
            unit="index",
            frequency="monthly",
            pit_classification=PIT_VERIFIED,
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_CPI_CORE_SA,
            unit="index",
            frequency="monthly",
            pit_classification=PIT_VERIFIED,
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_NONFARM_PAYROLLS_SA,
            unit="thousands_of_persons",
            frequency="monthly",
            pit_classification=PIT_VERIFIED,
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_UNEMPLOYMENT_RATE_SA,
            unit="percent",
            frequency="monthly",
            pit_classification=PIT_VERIFIED,
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_REAL_GDP_GROWTH_SAAR,
            unit="percent",
            frequency="quarterly",
            pit_classification=PIT_VERIFIED,
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_RETAIL_SALES_ADVANCE_SA,
            unit="millions_of_dollars",
            frequency="monthly",
            pit_classification=PIT_VERIFIED,
            release_time=_0830,
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=US_TREASURY_2Y_YIELD,
            unit="percent",
            frequency="daily",
            pit_classification=PIT_VERIFIED,
            # H.15 / daily par yield publishes ~16:15 ET; 18:00 ET keeps the
            # vintage on the safe side of look-ahead whether the ALFRED
            # vintage date is the publication day or FRED's ingestion day.
            release_time=time(18, 0),
            release_timezone=_ET,
        ),
        IndicatorSpec(
            series=UK_BANK_RATE,
            unit="percent",
            frequency="daily",
            pit_classification=PIT_UNVERIFIED,
            # MPC 決定は 12:00 ロンドンだが、IADB の日次行が載る時刻は保証が
            # ないため US_TREASURY_2Y_YIELD と同じく 18:00 で保守側に置く。
            release_time=time(18, 0),
            release_timezone=_LONDON,
        ),
        IndicatorSpec(
            series=UK_OIS_2Y,
            unit="percent",
            frequency="daily",
            pit_classification=PIT_UNVERIFIED,
            # カーブは翌営業日正午に公表される（観測日 D の値が D+1 12:00）。
            # forward collection では known_at は取得時刻なのでこの値は
            # 使われないが、vintage 復元経路が付いたときの基準として置く。
            release_time=time(12, 0),
            release_timezone=_LONDON,
        ),
        IndicatorSpec(
            series=UK_CPI_HEADLINE_YOY_NSA,
            unit="percent",
            frequency="monthly",
            pit_classification=PIT_UNVERIFIED,
            release_time=_0700,
            release_timezone=_LONDON,
        ),
        IndicatorSpec(
            series=UK_UNEMPLOYMENT_RATE_SA,
            unit="percent",
            frequency="monthly",
            pit_classification=PIT_UNVERIFIED,
            release_time=_0700,
            release_timezone=_LONDON,
        ),
        IndicatorSpec(
            series=UK_REAL_GDP_GROWTH_QOQ_SA,
            unit="percent",
            frequency="quarterly",
            pit_classification=PIT_UNVERIFIED,
            release_time=_0700,
            release_timezone=_LONDON,
        ),
        IndicatorSpec(
            series=EA_DEPOSIT_FACILITY_RATE,
            unit="percent",
            frequency="daily",
            pit_classification=PIT_UNVERIFIED,
            # 政策決定の公表は 14:15 CET。日次系列がポータルへ載る時刻は保証
            # がないため 18:00 で保守側に置く。
            release_time=time(18, 0),
            release_timezone=_BRUSSELS,
        ),
        IndicatorSpec(
            series=EA_YIELD_CURVE_2Y,
            unit="percent",
            frequency="daily",
            pit_classification=PIT_UNVERIFIED,
            # ポータルの更新は前営業日ぶんが翌日昼までに載る。
            release_time=time(12, 0),
            release_timezone=_BRUSSELS,
        ),
        IndicatorSpec(
            series=EA_HICP_HEADLINE_YOY_NSA,
            unit="percent",
            frequency="monthly",
            pit_classification=PIT_UNVERIFIED,
            release_time=_1100,
            release_timezone=_BRUSSELS,
        ),
        IndicatorSpec(
            series=EA_UNEMPLOYMENT_RATE_SA,
            unit="percent",
            frequency="monthly",
            pit_classification=PIT_UNVERIFIED,
            release_time=_1100,
            release_timezone=_BRUSSELS,
        ),
        IndicatorSpec(
            series=EA_REAL_GDP_GROWTH_QOQ_SCA,
            unit="percent",
            frequency="quarterly",
            pit_classification=PIT_UNVERIFIED,
            release_time=_1100,
            release_timezone=_BRUSSELS,
        ),
    )
}


def period_from_date(observation_date: date, frequency: str) -> str:
    """Reference-period string for an observation date (FRED-style dates mark
    the period start: 2026-07-01 is July 2026, 2026-04-01 is 2026Q2)."""
    if frequency == "daily":
        return observation_date.isoformat()
    if frequency == "monthly":
        return f"{observation_date.year}-{observation_date.month:02d}"
    quarter = (observation_date.month - 1) // 3 + 1
    return f"{observation_date.year}Q{quarter}"
