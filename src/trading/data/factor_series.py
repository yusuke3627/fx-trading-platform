"""通貨 × factor の PIT 観測列を macro 系列から組み立てる（ADR-018 の続き）。

`CurrencyStateService` は factor ごとに 1 本の観測列を受け取り、
`normalize_series` で「その通貨自身の履歴に対する robust z」へ落とす。
ここはその 1 本を `MacroObservationRepository` から作る層で、生の系列を
そのまま渡すと壊れる 3 点を吸収する。

**定常化。** 正規化は直近値が自分の履歴のどこにいるかを測るので、単調に
伸びる系列（米 CPI の index、雇用者数の水準）を入れると直近値が常に窓の
上限付近に来て z が正へ張り付く。前年同期比へ落としてから渡す。

**符号の統一。** 失業率のように「上がるほど通貨が弱い」系列は符号を反転
し、どの factor も「値が大きいほど通貨が強い」向きへ揃える。揃えなければ
`base - quote` の減算が意味を失う。

**vintage の畳み込み。** リポジトリが返すのは vintage 連鎖なので、1 観測
期間につき初報 1 点へ畳む。改定を採ると古い期間の改定が known_at 最新の
点になり、`normalize_series` がそれを「直近値」として z を取ってしまう。
lookback は各期間の初報を必ず含む幅に取ってあるので、窓に入った改定が
初報として拾われることはない。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from trading.data.macro.registry import (
    EA_DEPOSIT_FACILITY_RATE,
    EA_HICP_HEADLINE_YOY_NSA,
    EA_UNEMPLOYMENT_RATE_SA,
    INDICATORS,
    UK_BANK_RATE,
    UK_CPI_HEADLINE_YOY_NSA,
    UK_UNEMPLOYMENT_RATE_SA,
    US_CPI_HEADLINE_SA,
    US_TREASURY_2Y_YIELD,
    US_UNEMPLOYMENT_RATE_SA,
)
from trading.domain.economic import EconomicObservation
from trading.domain.money import Currency
from trading.intelligence.currency import CurrencyFactor
from trading.intelligence.immutable import freeze_mapping
from trading.intelligence.normalization import NormalizationConfig
from trading.storage.repository import MacroObservationRepository


class SeriesTransform(StrEnum):
    """生の観測値を factor の入力へ写す方法。"""

    # 公表値をそのまま使う（率・利回りなど、水準自体が平均回帰する系列）。
    LEVEL = "level"
    # 前年同期比（%）へ落とす（指数・水準など、単調に伸びる系列）。
    YEAR_OVER_YEAR = "year_over_year"


@dataclass(frozen=True)
class FactorInput:
    series: str
    # +1 は値が大きいほど通貨が強い、-1 はその逆。
    sign: int
    transform: SeriesTransform = SeriesTransform.LEVEL


# factor ごとに系列は 1 本。複数系列の合成は単位の違う値を 1 つの分布へ
# 混ぜることになり、正規化が意味を失う。
#
# 埋まっていない組み合わせは意図的な欠測で、供給側は空列を返す:
#   - JPY は macro 系列を持たない（BOJ 政策スコアと介入リスクが担う）
#   - USD の POLICY は FOMC 声明スコア（EventRepository 側）
#   - GBP / EUR の RATES は OIS / イールドカーブ proxy の収集待ち
# CurrencyState 側で欠測 factor は合成から外れ、coverage の減点になる。
DEFAULT_FACTOR_INPUTS: Mapping[tuple[Currency, CurrencyFactor], FactorInput] = (
    freeze_mapping(
        {
            (Currency.USD, CurrencyFactor.INFLATION): FactorInput(
                US_CPI_HEADLINE_SA, 1, SeriesTransform.YEAR_OVER_YEAR
            ),
            (Currency.USD, CurrencyFactor.GROWTH): FactorInput(
                US_UNEMPLOYMENT_RATE_SA, -1
            ),
            (Currency.USD, CurrencyFactor.RATES): FactorInput(US_TREASURY_2Y_YIELD, 1),
            (Currency.GBP, CurrencyFactor.POLICY): FactorInput(UK_BANK_RATE, 1),
            (Currency.GBP, CurrencyFactor.INFLATION): FactorInput(
                UK_CPI_HEADLINE_YOY_NSA, 1
            ),
            (Currency.GBP, CurrencyFactor.GROWTH): FactorInput(
                UK_UNEMPLOYMENT_RATE_SA, -1
            ),
            (Currency.EUR, CurrencyFactor.POLICY): FactorInput(
                EA_DEPOSIT_FACILITY_RATE, 1
            ),
            (Currency.EUR, CurrencyFactor.INFLATION): FactorInput(
                EA_HICP_HEADLINE_YOY_NSA, 1
            ),
            (Currency.EUR, CurrencyFactor.GROWTH): FactorInput(
                EA_UNEMPLOYMENT_RATE_SA, -1
            ),
        }
    )
)

_PERIODS_PER_YEAR: Mapping[str, int] = freeze_mapping(
    {"daily": 252, "monthly": 12, "quarterly": 4}
)

# window の外側に確保する余裕（観測期間の数）。欠測・収集停止・改定で
# 実際に届く点数が窓を下回るのを防ぐ。
_LOOKBACK_MARGIN_PERIODS = 12

_DAYS_PER_YEAR = 365.25


class MacroFactorSeries:
    """`MacroObservationRepository` を `FactorSeriesSource` として見せる。

    `normalization` は読み出し幅の算出に使う。`CurrencyStateService` へ渡す
    のと同じ設定を渡すこと — 窓より狭い lookback で読むと、正規化が
    min_observations に届かず factor が丸ごと欠測になる。
    """

    def __init__(
        self,
        observations: MacroObservationRepository,
        normalization: NormalizationConfig,
        inputs: Mapping[
            tuple[Currency, CurrencyFactor], FactorInput
        ] = DEFAULT_FACTOR_INPUTS,
    ) -> None:
        self._observations = observations
        self._normalization = normalization
        self._inputs = dict(inputs)

    def series(
        self, currency: Currency, factor: CurrencyFactor, now: datetime
    ) -> Sequence[tuple[datetime, float]]:
        factor_input = self._inputs.get((currency, factor))
        if factor_input is None:
            return ()

        frequency = INDICATORS[factor_input.series].frequency
        since = now - self._lookback(frequency, factor_input.transform)
        first_prints = _first_print_per_period(
            self._observations.known_before(factor_input.series, now, since)
        )
        if factor_input.transform is SeriesTransform.YEAR_OVER_YEAR:
            points = _year_over_year(first_prints)
        else:
            points = [
                (row.known_at, float(row.value)) for row in first_prints.values()
            ]
        return [
            (at, factor_input.sign * value)
            for at, value in points
            if math.isfinite(value)
        ]

    def _lookback(self, frequency: str, transform: SeriesTransform) -> timedelta:
        per_year = _PERIODS_PER_YEAR[frequency]
        periods = self._normalization.window + _LOOKBACK_MARGIN_PERIODS
        if transform is SeriesTransform.YEAR_OVER_YEAR:
            # 前年同期比は最古の点にも 1 年前の相手が要る。
            periods += per_year
        return timedelta(days=math.ceil(periods / per_year * _DAYS_PER_YEAR))


def _first_print_per_period(
    vintages: Sequence[EconomicObservation],
) -> dict[str, EconomicObservation]:
    """観測期間ごとの初報。リポジトリは known_at 昇順で返す。"""
    first: dict[str, EconomicObservation] = {}
    for row in vintages:
        first.setdefault(row.observation_period, row)
    return first


def _year_over_year(
    first_prints: Mapping[str, EconomicObservation],
) -> list[tuple[datetime, float]]:
    points: list[tuple[datetime, float]] = []
    for period, row in first_prints.items():
        # 1 年前の相手も初報なので、この点の known_at 時点で既知。
        base = first_prints.get(_previous_year(period))
        if base is None or base.value == 0:
            continue
        points.append((row.known_at, float((row.value / base.value - 1) * 100)))
    return points


def _previous_year(period: str) -> str:
    """`YYYY-MM` / `YYYYQn` / `YYYY-MM-DD` の年だけを 1 つ戻す。"""
    return f"{int(period[:4]) - 1:04d}{period[4:]}"
