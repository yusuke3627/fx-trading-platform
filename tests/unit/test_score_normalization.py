"""スコア正規化（設計書 34.5A、ADR-018）。

All values are fictional test data.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading.intelligence.normalization import NormalizationConfig, normalize_series

T0 = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
CONFIG = NormalizationConfig(window=20, min_observations=5, clip_sigma=3.0)


def series(values: list[float], start: datetime = T0) -> list[tuple[datetime, float]]:
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def test_future_observations_do_not_move_the_score():
    # PIT: now より後の観測は統計にも最新値にも入らない。
    rows = series([1.0, 1.2, 0.9, 1.1, 1.0, 1.05])
    now = rows[-1][0]
    with_future = [*rows, (now + timedelta(days=1), 99.0)]

    assert normalize_series(rows, now, CONFIG) == normalize_series(
        with_future, now, CONFIG
    )


def test_window_is_rolling_not_whole_history():
    # 遠い過去の外れ値は window から外れる。全期間で fit していれば
    # 同じ最新値でも別のスコアになる。
    recent = [1.0, 1.2, 0.9, 1.1, 1.0, 1.3]
    rows = series([-500.0] * 30 + recent)
    settings = NormalizationConfig(window=6, min_observations=5)

    windowed = normalize_series(rows, rows[-1][0], settings)
    recent_rows = series(recent)
    recent_only = normalize_series(recent_rows, recent_rows[-1][0], settings)

    assert windowed is not None and recent_only is not None
    assert windowed.value == recent_only.value


def test_too_few_observations_yield_no_score():
    rows = series([1.0, 2.0, 3.0])

    assert normalize_series(rows, rows[-1][0], CONFIG) is None


def test_constant_series_yields_no_score():
    # 散らばりの無い系列は尺度を語れない。0（中立という観測）に潰さず
    # None を返し、呼び出し側で coverage 不足として扱わせる。
    rows = series([2.0] * 10)

    assert normalize_series(rows, rows[-1][0], CONFIG) is None


def test_score_is_bounded_and_signed():
    high = series([1.0, 1.1, 0.9, 1.0, 1.05, 20.0])
    low = series([1.0, 1.1, 0.9, 1.0, 1.05, -20.0])

    up = normalize_series(high, high[-1][0], CONFIG)
    down = normalize_series(low, low[-1][0], CONFIG)

    assert up is not None and down is not None
    assert 0 < up.value <= 1
    assert -1 <= down.value < 0


def test_distributions_stay_comparable_across_scales():
    # 単位も振れ幅も違う 2 系列（% と bp）が、分布内で同じ相対位置なら
    # 同じスコアになる — これが base - quote の前提（設計書 §12.2A）。
    percent = [1.0, 1.1, 0.9, 1.0, 1.2, 1.3]
    basis_points = [v * 100 for v in percent]

    percent_rows = series(percent)
    bp_rows = series(basis_points)
    a = normalize_series(percent_rows, percent_rows[-1][0], CONFIG)
    b = normalize_series(bp_rows, bp_rows[-1][0], CONFIG)

    assert a is not None and b is not None
    assert a.value == b.value


def test_clip_bounds_extreme_moves():
    # 外れ値がどれだけ大きくてもスコアは飽和する。1 通貨のデータ異常が
    # portfolio 全体の方向感を支配しないための上限。
    moderate = series([1.0, 1.1, 0.9, 1.0, 1.05, 5.0])
    extreme = series([1.0, 1.1, 0.9, 1.0, 1.05, 5_000.0])

    a = normalize_series(moderate, moderate[-1][0], CONFIG)
    b = normalize_series(extreme, extreme[-1][0], CONFIG)

    assert a is not None and b is not None
    # clip_sigma=3 に張り付いた z の tanh(3/3)。
    assert a.value == b.value == Decimal("0.761594")


def test_reports_the_observation_it_fitted_through():
    rows = series([1.0, 1.1, 0.9, 1.0, 1.05, 1.4])

    result = normalize_series(rows, rows[-1][0], CONFIG)

    assert result is not None
    assert result.fitted_through == rows[-1][0]
    assert result.observations == 6


def test_minimum_must_fit_inside_the_window():
    # window より広い最小観測数は、window が捨てる行で満たされてしまい、
    # 契約より少ない観測でスコアが出る。設定境界で弾く。
    with pytest.raises(ValueError, match="min_observations"):
        NormalizationConfig(window=3, min_observations=5)


def test_non_finite_observations_are_not_counted():
    # 欠測を NaN で表す供給元があり、そのまま通すと clip が最大側へ張り付き
    # 「最も強い買いシグナル」に化ける。観測として数えない。
    clean = series([1.0, 1.1, 0.9, 1.0, 1.05, 1.2])
    polluted = [*clean, (T0 + timedelta(days=6), float("nan"))]
    now = T0 + timedelta(days=6)

    assert normalize_series(polluted, now, CONFIG) == normalize_series(
        clean, now, CONFIG
    )


def test_all_non_finite_series_yields_no_score():
    rows = series([float("nan")] * 10)

    assert normalize_series(rows, rows[-1][0], CONFIG) is None


def test_same_instant_observations_keep_their_supplied_order():
    # 同一 known_at の観測（同時収集された改訂値など）を値で並べ替えると、
    # window の末尾に最大値が来て「最新」として扱われ、スコアが正へ偏る。
    at = T0 + timedelta(days=5)
    base = series([1.0, 1.1, 0.9, 1.0, 1.05])
    ascending = [*base, (at, 0.5), (at, 2.0)]
    descending = [*base, (at, 2.0), (at, 0.5)]

    first = normalize_series(ascending, at, CONFIG)
    second = normalize_series(descending, at, CONFIG)

    assert first is not None and second is not None
    # 供給順の最後がそれぞれ最新なので、結果は一致しない。
    assert first.value != second.value
    assert first.value > 0 > second.value


def test_huge_but_finite_values_do_not_become_a_max_score():
    # 入力が有限でも、中央値の加算がオーバーフローすると z が NaN になり、
    # clip が上限を選んで最大の買いスコアに化ける。
    rows = series([1e308, 1.1e308, 1.2e308, 1.05e308, 1.15e308, 1.3e308])

    result = normalize_series(rows, rows[-1][0], CONFIG)

    assert result is None or result.value != Decimal("0.761594")


def test_opposite_sign_extremes_still_normalize():
    # 中央の2値が異符号の巨大値だと差がオーバーフローする。和・差の安全な
    # 側を選ばないと、有限値だけの系列が観測不能に落ちる。
    rows = series([-1e308, 1e308, -0.9e308, 0.9e308, -0.5e308, 0.8e308])

    result = normalize_series(rows, rows[-1][0], CONFIG)

    assert result is not None
    assert -1 <= result.value <= 1
