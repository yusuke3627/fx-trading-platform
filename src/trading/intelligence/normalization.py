"""Point-in-time score normalization（設計書 v2.1 §12.2A、ADR-018）。

通貨間で `base - quote` が意味を持つには、4 通貨の生スコアを同一尺度へ
校正する必要がある。USD だけ観測数が多いことを理由に振れ幅が機械的に
大きくならないよう、各系列を自分自身の分布で標準化してから有界変換する。

    raw → PIT rolling robust z（median / MAD）→ clip → tanh → [-1, 1]

**統計は known_at <= now の window 内だけで計算する。** 全期間で fit した
パラメータを過去へ適用するのは look-ahead であり、backtest の成績を実運用
では再現できない値へ持ち上げる。

`walk-forward calibration to forward risk-normalized return`（設計書の
pipeline 最終段）は本モジュールの範囲外 — forward return の紐付けは
research 側の校正作業で、ここは「比較可能な尺度へ載せる」までを担う。
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# MAD を正規分布の標準偏差へ合わせる定数（1 / Φ⁻¹(3/4)）。
_MAD_TO_SIGMA = 1.4826

# スコアの丸め桁。通貨間の減算を安定させるために固定する。
_SCORE_EXPONENT = Decimal("0.000001")


class NormalizationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # rolling window に入れる観測数の上限。
    window: int = Field(default=60, gt=0)
    # これを下回る window では分布を語れない（正規化しない）。
    min_observations: int = Field(default=20, gt=1)
    # z を切り詰める幅。tanh の入力スケールもこれで割って揃える。
    clip_sigma: float = Field(default=3.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _window_holds_the_minimum(self) -> NormalizationConfig:
        if self.min_observations > self.window:
            raise ValueError(
                "min_observations must not exceed window; a wider minimum "
                "would be satisfied by rows the window then drops, scoring "
                "on fewer observations than the contract promises"
            )
        return self


class NormalizedScore(BaseModel):
    """1 系列の正規化結果と、その根拠。

    `observations` と `fitted_through` は confidence の材料であって score
    の一部ではない（設計書 §12.2A「confidence は score magnitude と別変数」）。
    """

    model_config = ConfigDict(frozen=True)

    value: Decimal
    observations: int
    # 統計に使った最新 known_at。freshness 判定の起点。
    fitted_through: datetime


def normalize_series(
    series: Sequence[tuple[datetime, float]],
    now: datetime,
    config: NormalizationConfig | None = None,
) -> NormalizedScore | None:
    """`now` 時点で見えている観測だけから、最新値を [-1, 1] へ写す。

    None を返すのは「尺度を語れない」場合 — 観測が足りない、または window
    内が定数（MAD = 0）で散らばりが無い。呼び出し側はこれを 0（中立）に
    潰さず、coverage 不足として confidence を下げる。

    非有限値（NaN / Inf）は観測として数えない。欠測を NaN で表す供給元が
    あり、そのまま通すと clip が最大側へ張り付いて「最も強い買いシグナル」
    に化ける — 異常データが最大確信の売買判断になる経路を入口で断つ。
    """
    settings = config or NormalizationConfig()
    # known_at だけで並べる（安定ソート）。タプルの自然順序に任せると同一
    # 時刻の観測が値の昇順に並び、window の末尾＝「最新」として最大値が
    # 選ばれてスコアが正へ偏る。同時刻の行は供給された順序を最新とする。
    visible = sorted(
        (
            (at, value)
            for at, value in series
            if at <= now and math.isfinite(value)
        ),
        key=lambda row: row[0],
    )
    if len(visible) < settings.min_observations:
        return None

    window = visible[-settings.window :]
    values = [value for _, value in window]
    median = _median(values)
    mad = _median([abs(value - median) for value in values])
    if mad == 0:
        return None

    latest_at, latest = window[-1]
    z = (latest - median) / (mad * _MAD_TO_SIGMA)
    clipped = max(-settings.clip_sigma, min(settings.clip_sigma, z))
    bounded = math.tanh(clipped / settings.clip_sigma)
    return NormalizedScore(
        value=Decimal(str(bounded)).quantize(_SCORE_EXPONENT),
        observations=len(window),
        fitted_through=latest_at,
    )


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
