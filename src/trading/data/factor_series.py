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
読み出し窓は known_at で切られるため、窓より前に初報が出た期間には改定
しか残らない。そういう期間は落とす。

**順序の明示。** forward collector は初回収集で全履歴へ同じ取得時刻を
known_at として付ける。`normalize_series` は known_at で安定ソートし、
同時刻の並びは供給側の順序をそのまま「最新」とするので、ここで観測期間順
に並べておかないと、リポジトリが返す UUID 順の最後＝任意の過去期間が
直近値になる。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from trading.data.macro.registry import (
    EA_HICP_HEADLINE_YOY_NSA,
    EA_UNEMPLOYMENT_RATE_SA,
    EA_YIELD_CURVE_2Y,
    INDICATORS,
    UK_CPI_HEADLINE_YOY_NSA,
    UK_OIS_2Y,
    UK_UNEMPLOYMENT_RATE_SA,
    US_CPI_HEADLINE_SA,
    US_TREASURY_2Y_YIELD,
    US_UNEMPLOYMENT_RATE_SA,
)
from trading.data.policy.risk_windows import BANK_CURRENCIES
from trading.data.policy.scoring import EVENT_TYPES, SCORING_VERSION
from trading.domain.economic import EconomicObservation
from trading.domain.money import Currency
from trading.intelligence.currency import CurrencyFactor
from trading.intelligence.immutable import freeze_mapping
from trading.intelligence.normalization import NormalizationConfig
from trading.storage.repository import EventRepository, MacroObservationRepository


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
# POLICY はここに載せない。中銀の声明スコアが正本で、供給元は
# PolicyScoreFactorSeries（ADR-021）。政策金利の水準を代わりに置くと、
# 据え置き期間は窓が同値で埋まって正規化が語れなくなるうえ、金利パスの
# 情報は RATES の 2 年点が既に持っている。
#
# JPY は macro 系列を持たない（BOJ 声明スコアと介入リスクが担う）。
# 欠測 factor は CurrencyState 側で合成から外れ、coverage の減点になる。
#
# RATES は 3 通貨とも 2 年点で揃える。年限が違う点どうしを引き算しても
# 金利差にならない（ADR-020）。
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
            (Currency.GBP, CurrencyFactor.INFLATION): FactorInput(
                UK_CPI_HEADLINE_YOY_NSA, 1
            ),
            (Currency.GBP, CurrencyFactor.GROWTH): FactorInput(
                UK_UNEMPLOYMENT_RATE_SA, -1
            ),
            (Currency.GBP, CurrencyFactor.RATES): FactorInput(UK_OIS_2Y, 1),
            (Currency.EUR, CurrencyFactor.INFLATION): FactorInput(
                EA_HICP_HEADLINE_YOY_NSA, 1
            ),
            (Currency.EUR, CurrencyFactor.GROWTH): FactorInput(
                EA_UNEMPLOYMENT_RATE_SA, -1
            ),
            (Currency.EUR, CurrencyFactor.RATES): FactorInput(EA_YIELD_CURVE_2Y, 1),
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
            self._observations.known_before(factor_input.series, now, since),
            _period_at(frequency, since),
        )
        if factor_input.transform is SeriesTransform.YEAR_OVER_YEAR:
            points = _year_over_year(first_prints)
        else:
            points = {
                period: (row.known_at, float(row.value))
                for period, row in first_prints.items()
            }
        return [
            (known_at, factor_input.sign * value)
            for _, (known_at, value) in sorted(
                points.items(), key=lambda point: (point[1][0], point[0])
            )
            if math.isfinite(value)
        ]

    def read_windows(self) -> dict[str, timedelta]:
        """系列ごとの読み出し幅。

        replay 用に行を 1 回で凍結する側（`data/features.py`）が同じ窓を
        張るために要る。窓が狭いと、凍結した行の集合が実際の snapshot が
        読む範囲より小さくなり、replay だけ factor が欠測する。
        """
        return {
            factor_input.series: self._lookback(
                INDICATORS[factor_input.series].frequency, factor_input.transform
            )
            for factor_input in self._inputs.values()
        }

    def _lookback(self, frequency: str, transform: SeriesTransform) -> timedelta:
        per_year = _PERIODS_PER_YEAR[frequency]
        periods = self._normalization.window + _LOOKBACK_MARGIN_PERIODS
        if transform is SeriesTransform.YEAR_OVER_YEAR:
            # 前年同期比は最古の点にも 1 年前の相手が要る。
            periods += per_year
        return timedelta(days=math.ceil(periods / per_year * _DAYS_PER_YEAR))


def _first_print_per_period(
    vintages: Sequence[EconomicObservation], oldest_period: str
) -> dict[str, EconomicObservation]:
    """観測期間ごとの初報。リポジトリは known_at 昇順で返す。

    `oldest_period` より古い期間は落とす。読み出し窓は known_at で切られる
    ので、窓が開く前に初報が出た期間には改定しか残っていない。年次ベンチ
    マーク改定のように古い期間へ遡る改定を初報として採ると、その古い値が
    改定時刻の観測として正規化窓へ入る。
    """
    first: dict[str, EconomicObservation] = {}
    for row in vintages:
        if row.observation_period < oldest_period:
            continue
        first.setdefault(row.observation_period, row)
    return first


def _year_over_year(
    first_prints: Mapping[str, EconomicObservation],
) -> dict[str, tuple[datetime, float]]:
    points: dict[str, tuple[datetime, float]] = {}
    for period, row in first_prints.items():
        # 1 年前の相手も初報なので、この点の known_at 時点で既知。
        base = first_prints.get(_previous_year(period))
        if base is None or base.value == 0:
            continue
        points[period] = (row.known_at, float((row.value / base.value - 1) * 100))
    return points


def _period_at(frequency: str, moment: datetime) -> str:
    """`moment` を含む観測期間のラベル。registry の頻度が形式を決める。"""
    if frequency == "monthly":
        return f"{moment.year:04d}-{moment.month:02d}"
    if frequency == "quarterly":
        return f"{moment.year:04d}Q{(moment.month - 1) // 3 + 1}"
    return moment.strftime("%Y-%m-%d")


def _previous_year(period: str) -> str:
    """`YYYY-MM` / `YYYYQn` / `YYYY-MM-DD` の年だけを 1 つ戻す。"""
    return f"{int(period[:4]) - 1:04d}{period[4:]}"


# 中銀声明スコアの採点が定義されているのは FED / BOJ だけ。BOE / ECB は
# 採点対象外なので、GBP / EUR の POLICY は欠測になる。
EVENT_TYPE_BY_CURRENCY: Mapping[Currency, str] = freeze_mapping(
    {BANK_CURRENCIES[bank]: event_type for bank, event_type in EVENT_TYPES.items()}
)

# 会合は年 8 回なので 1 年ぶんでも複数回入る。転記の遅れを吸収する余裕を
# 見て 400 日。最新の 1 件しか使わないため窓を広く取る意味は薄い。
POLICY_LOOKBACK = timedelta(days=400)


class PolicyScoreFactorSeries:
    """中銀の声明スコアを POLICY factor の観測列として供給する。

    値は `data/policy/scoring.py` の配点表で採った [-2, +2] のスコアで、
    中銀をまたいで同じ尺度に載っている。`CurrencyStateService` はこの
    factor を正規化せず上限で割るだけにする（ADR-021）。
    """

    def __init__(self, events: EventRepository) -> None:
        self._events = events

    def series(
        self, currency: Currency, factor: CurrencyFactor, now: datetime
    ) -> Sequence[tuple[datetime, float]]:
        event_type = EVENT_TYPE_BY_CURRENCY.get(currency)
        if factor is not CurrencyFactor.POLICY or event_type is None:
            return ()
        # 配点を見直すと、scoring.py は過去会合を書き換えずに新しい版の
        # イベントとして再投入する。同じ会合が複数の版で、同じ known_at の
        # まま store に並ぶ。読み手が版を選ばないと、同時刻の並び順（events
        # の ORDER BY は known_at だけ）次第で旧版が「直近」になり、政策の
        # 向きが反転しうる。このビルドが計算する版だけを採る。
        return [
            (event.known_at, float(event.payload["score"]))
            for event in self._events.known_before(
                now, event_type=event_type, since=now - POLICY_LOOKBACK
            )
            if event.payload.get("scoring_version") == SCORING_VERSION
        ]
