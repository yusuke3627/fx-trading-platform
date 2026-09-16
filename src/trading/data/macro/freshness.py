"""収集した最新期間の古さを、系列の公表頻度と公表ラグから判定する。

dissemination API は dataset の配信終了後も古い値を返し続けるため、
取得成功・新規保存 0 件だけでは正常な再取得と更新停止を区別できない。
更新停止を長期間見逃さないよう、取得した最新期間そのものの古さを検査する。
"""
from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable, Mapping
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from trading.data.macro.registry import INDICATORS, IndicatorSpec
from trading.domain.economic import EconomicObservation

STALENESS_LIMIT_DAYS: dict[str, int] = {"daily": 21, "monthly": 75, "quarterly": 180}


def staleness_limit_days(spec: IndicatorSpec) -> int:
    if spec.max_staleness_days is not None:
        return spec.max_staleness_days
    return STALENESS_LIMIT_DAYS[spec.frequency]


def period_end(period: str) -> date:
    if len(period) == 10:
        return date.fromisoformat(period)
    if "Q" in period:
        year, quarter = map(int, period.split("Q"))
        month = quarter * 3
    else:
        year, month = map(int, period.split("-"))
    return date(year, month, monthrange(year, month)[1])


def merge_latest_periods(
    latest: Mapping[str, str], observations: Iterable[EconomicObservation]
) -> dict[str, str]:
    merged = dict(latest)
    for observation in observations:
        series = observation.series
        period = observation.observation_period
        if series not in merged or period_end(period) > period_end(merged[series]):
            merged[series] = period
    return merged


class StaleSeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    series: str
    latest_period: str
    age_days: int
    limit_days: int


def stale_series(latest: Mapping[str, str], now: datetime) -> tuple[StaleSeries, ...]:
    stale: list[StaleSeries] = []
    for series, period in sorted(latest.items()):
        age_days = (now.date() - period_end(period)).days
        limit_days = staleness_limit_days(INDICATORS[series])
        if age_days > limit_days:
            stale.append(StaleSeries(
                series=series,
                latest_period=period,
                age_days=age_days,
                limit_days=limit_days,
            ))
    return tuple(stale)
