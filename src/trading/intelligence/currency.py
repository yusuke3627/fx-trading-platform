"""Currency-first state: CurrencyState と PairState（設計書 v2.1 §12、ADR-018）。

方向感は通貨単位で持ち、ペアはその差として導く。

    pair.directional_score = base.directional_score - quote.directional_score

減算が意味を持つのは両辺が同一尺度に載っているときだけなので、factor は
`normalization.normalize_series` を通してから合成する。

confidence は score magnitude と独立の変数（設計書 §12.2A）。データが
足りない通貨のスコアを膨らませて「強い方向感」に見せることはせず、値は
中立のまま confidence を下げる。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading.domain.instrument import InstrumentSpec
from trading.domain.money import Currency
from trading.intelligence.normalization import (
    NormalizationConfig,
    NormalizedScore,
    normalize_series,
)
from trading.intelligence.regime import RegimeLabel

_SCORE_EXPONENT = Decimal("0.000001")


class CurrencyFactor(StrEnum):
    """通貨の方向感を構成する観測系列の種類（設計書 §12.1）。"""

    POLICY = "policy"
    GROWTH = "growth"
    INFLATION = "inflation"
    RATES = "rates"
    RISK_SENTIMENT = "risk_sentiment"


class FactorSeriesSource(Protocol):
    """通貨 × factor の PIT 観測列を供給する。

    返すのは `(known_at, raw_value)` の列で、`now` より後の観測を含んで
    はならない（正規化側でも切るが、供給側が PIT の一次責任を負う）。
    系列を持たない組み合わせは空列を返す — これは coverage 不足であって
    「中立」ではない。
    """

    def series(
        self, currency: Currency, factor: CurrencyFactor, now: datetime
    ) -> Sequence[tuple[datetime, float]]: ...


class MappingFactorSeries:
    """メモリ上の `(currency, factor) -> 観測列` 供給元。

    replay / テストの配線と、PIT リポジトリ実装が入るまでの受け皿。
    """

    def __init__(
        self,
        series: Mapping[tuple[Currency, CurrencyFactor], Sequence[tuple[datetime, float]]],
    ) -> None:
        self._series = dict(series)

    def series(
        self, currency: Currency, factor: CurrencyFactor, now: datetime
    ) -> Sequence[tuple[datetime, float]]:
        rows = self._series.get((currency, factor), ())
        return [(at, value) for at, value in rows if at <= now]


class CurrencyScoreConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # factor の重み。合計は正規化されるので相対値でよい。欠測 factor の
    # 重みは directional_score の合成から外れ、coverage の減点になる。
    weights: dict[CurrencyFactor, float] = Field(
        default_factory=lambda: {
            CurrencyFactor.POLICY: 1.0,
            CurrencyFactor.GROWTH: 1.0,
            CurrencyFactor.INFLATION: 1.0,
            CurrencyFactor.RATES: 1.0,
            CurrencyFactor.RISK_SENTIMENT: 1.0,
        }
    )
    normalization: NormalizationConfig = NormalizationConfig()
    # これより古い観測しか無い factor は freshness 減点を受ける。
    freshness_full_hours: float = Field(default=48.0, gt=0, allow_inf_nan=False)
    # 減点が底を打つまでの経過（ここに達した factor の freshness は 0）。
    freshness_zero_hours: float = Field(default=336.0, gt=0, allow_inf_nan=False)
    # 両 leg の打ち消し合いに掛ける confidence 減点の強さ（0 で無効）。
    pair_cancellation_penalty: float = Field(
        default=0.5, ge=0, le=1, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def _usable(self) -> CurrencyScoreConfig:
        if sum(self.weights.values()) <= 0:
            raise ValueError("weights must sum to a positive value")
        if any(weight < 0 for weight in self.weights.values()):
            raise ValueError("factor weights must not be negative")
        if self.freshness_zero_hours <= self.freshness_full_hours:
            raise ValueError(
                "freshness_zero_hours must exceed freshness_full_hours; "
                "otherwise the decay has no span and every observation reads "
                "as stale the moment it ages past the full mark"
            )
        return self


class CurrencyState(BaseModel):
    model_config = ConfigDict(frozen=True)

    currency: Currency
    directional_score: Decimal
    # 欠測 factor は None のまま持つ。0 は「中立という観測がある」ことを
    # 意味してしまい、無いこととは違う。
    factor_scores: dict[CurrencyFactor, Decimal | None]
    confidence: Decimal
    regimes: frozenset[RegimeLabel] = frozenset()
    intervention_risk: Decimal | None = None
    known_at: datetime

    def score(self, factor: CurrencyFactor) -> Decimal | None:
        return self.factor_scores.get(factor)


class PairState(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    base: CurrencyState
    quote: CurrencyState
    directional_score: Decimal
    confidence: Decimal
    known_at: datetime


class CurrencyStateService:
    """PIT 観測列から CurrencyState / PairState を組み立てる。

    event risk はここに載せない — 通貨 scope 付きの gate は
    `EventRiskCalendar.mode_for_instrument`（ADR-017）が既に担っており、
    同じ判断を二箇所に持たせない（設計書 §12.3 の「risk state を
    directional score から分離する」の適用）。
    """

    def __init__(
        self,
        source: FactorSeriesSource,
        config: CurrencyScoreConfig | None = None,
    ) -> None:
        self._source = source
        self._config = config or CurrencyScoreConfig()

    def state(
        self,
        currency: Currency,
        now: datetime,
        *,
        regimes: frozenset[RegimeLabel] = frozenset(),
        intervention_risk: Decimal | None = None,
    ) -> CurrencyState:
        scores: dict[CurrencyFactor, Decimal | None] = {}
        normalized: dict[CurrencyFactor, NormalizedScore] = {}
        for factor in CurrencyFactor:
            result = normalize_series(
                self._source.series(currency, factor, now),
                now,
                self._config.normalization,
            )
            scores[factor] = result.value if result else None
            if result is not None:
                normalized[factor] = result

        return CurrencyState(
            currency=currency,
            directional_score=self._directional(normalized),
            factor_scores=scores,
            confidence=self._confidence(normalized, now),
            regimes=regimes,
            intervention_risk=intervention_risk,
            known_at=now,
        )

    def pair_state(self, spec: InstrumentSpec, now: datetime) -> PairState:
        """両 leg の state を組んで射影する。regime / intervention を載せる
        場合は `state()` を2回呼んで `project()` する。"""
        return self.project(
            spec,
            self.state(spec.base_currency, now),
            self.state(spec.quote_currency, now),
        )

    def project(
        self, spec: InstrumentSpec, base: CurrencyState, quote: CurrencyState
    ) -> PairState:
        return PairState(
            symbol=spec.symbol,
            base=base,
            quote=quote,
            directional_score=base.directional_score - quote.directional_score,
            confidence=self._pair_confidence(base, quote),
            # 両 leg の古い方。ペアは遅れている leg の分しか語れない。
            known_at=min(base.known_at, quote.known_at),
        )

    def _directional(self, normalized: Mapping[CurrencyFactor, NormalizedScore]) -> Decimal:
        """利用可能な factor の加重平均。

        欠測 factor は合成から外すだけで、残った factor を薄めない — 薄める
        と「データが少ないほど中立に見える」ことになり、方向感の欠如と
        観測の欠如が区別できなくなる。両者の区別は confidence が持つ。
        """
        weights = self._config.weights
        total = sum(weights.get(factor, 0.0) for factor in normalized)
        if total <= 0:
            # 観測できた factor がゼロ、または重み 0 の factor しか無い。
            # 方向感を語れないので中立（confidence 側が 0 を語る）。
            return Decimal(0)
        weighted = sum(
            Decimal(str(weights.get(factor, 0.0))) * score.value
            for factor, score in normalized.items()
        )
        return (weighted / Decimal(str(total))).quantize(_SCORE_EXPONENT)

    def _confidence(
        self, normalized: Mapping[CurrencyFactor, NormalizedScore], now: datetime
    ) -> Decimal:
        """coverage × freshness。score magnitude とは独立（設計書 §12.2A）。

        coverage は「重みつきでどれだけの factor が観測できたか」、
        freshness は「その観測がどれだけ新しいか」。片方でも欠ければ
        confidence は下がるが、directional_score は動かない。
        """
        weights = self._config.weights
        expected = sum(weights.values())
        covered = 0.0
        for factor, score in normalized.items():
            weight = weights.get(factor, 0.0)
            covered += weight * self._freshness(score.fitted_through, now)
        return (Decimal(str(covered)) / Decimal(str(expected))).quantize(_SCORE_EXPONENT)

    def _freshness(self, fitted_through: datetime, now: datetime) -> float:
        age_hours = max((now - fitted_through) / timedelta(hours=1), 0.0)
        full = self._config.freshness_full_hours
        zero = self._config.freshness_zero_hours
        if age_hours <= full:
            return 1.0
        if age_hours >= zero:
            return 0.0
        return (zero - age_hours) / (zero - full)

    def _pair_confidence(self, base: CurrencyState, quote: CurrencyState) -> Decimal:
        """両 leg の弱い方を起点に、打ち消し合いの分だけ減点する。

        単純平均にしないのは、片方の leg が観測不足なら pair の差もその
        不確かさを引き継ぐため。加えて、両 leg が同方向に強いときの差
        （例: 双方 hawkish で net が小さい）は大きな値どうしの引き算で、
        同じ絶対値の差でも相対的な不確かさが大きい — その分を減点する。
        """
        floor = min(base.confidence, quote.confidence)
        magnitude = abs(base.directional_score) + abs(quote.directional_score)
        if magnitude == 0:
            return floor
        spread = abs(base.directional_score - quote.directional_score)
        cancellation = (magnitude - spread) / magnitude
        penalty = Decimal(str(self._config.pair_cancellation_penalty)) * cancellation
        return (floor * (Decimal(1) - penalty)).quantize(_SCORE_EXPONENT)
