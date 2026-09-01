"""USD perturbation 感度ハーネス（設計書 §12.2 / §34.5A）。

合成の通貨 state のみを使う。σ を制御しやすいよう、USD の score 系列は
母標準偏差が切りの良い値になるものを選ぶ。
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading.backtest.usd_perturbation_study import (
    PERTURBATION_STEPS,
    USD_PAIR_SPECS,
    measure,
    perturbed_usd,
    render,
    usd_sigma,
)
from trading.domain.money import Currency
from trading.intelligence.currency import CurrencyScoreConfig, CurrencyState

NOW = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)

CONFIG = CurrencyScoreConfig()


def state(currency: Currency, score: str) -> CurrencyState:
    return CurrencyState(
        currency=currency,
        directional_score=Decimal(score),
        factor_scores={},
        confidence=Decimal("0.5"),
        known_at=NOW,
    )


def days(*usd_scores: str, others: dict[Currency, str] | None = None) -> list[dict]:
    """USD の score 系列を 1 日 1 snapshot に展開する。"""
    legs = others or {Currency.JPY: "0"}
    return [
        {Currency.USD: state(Currency.USD, score)}
        | {currency: state(currency, value) for currency, value in legs.items()}
        for score in usd_scores
    ]


def flips_for(result, symbol: str) -> dict[Decimal, int]:
    return dict(next(pair for pair in result.pairs if pair.symbol == symbol).flips)


def test_sigma_is_the_population_stdev_of_the_usd_series() -> None:
    # pstdev([0.05, -0.15]) = 0.1
    assert usd_sigma(days("0.05", "-0.15")) == Decimal("0.1")


def test_a_shift_that_crosses_zero_flips_and_one_that_does_not_does_not() -> None:
    # σ = 0.1。k = -1.0 の shift -0.1 は day1（+0.05）だけを符号反転させる。
    # day2（-0.15）は -0.25 になるだけで方向は変わらない。
    result = measure(days("0.05", "-0.15"), CONFIG)

    flips = flips_for(result, "USDJPY")

    assert flips[Decimal("-1.0")] == 1
    assert flips[Decimal("-0.5")] == 0  # shift -0.05 では 0.05 → 0.00 止まり
    assert flips[Decimal("1.0")] == 0  # 同方向への shift は flip しない


def test_landing_exactly_on_zero_is_not_a_flip() -> None:
    # 摂動後が 0（方向感なし）になった日は反転とは違う。
    result = measure(days("0.05", "-0.15"), CONFIG)

    assert flips_for(result, "USDJPY")[Decimal("-0.5")] == 0


def test_usd_on_the_quote_side_moves_the_pair_the_opposite_way() -> None:
    # EURUSD は USD が quote。USD を負方向へ動かすとペアは正方向へ動く。
    result = measure(
        days("0.05", "-0.15", others={Currency.EUR: "0"}), CONFIG
    )

    assert flips_for(result, "EURUSD")[Decimal("-1.0")] == 1
    assert flips_for(result, "EURUSD")[Decimal("1.0")] == 0


def test_the_perturbed_score_is_clamped_to_the_unit_interval() -> None:
    assert perturbed_usd(
        state(Currency.USD, "0.9"), Decimal("0.2")
    ).directional_score == Decimal(1)
    assert perturbed_usd(
        state(Currency.USD, "-0.9"), Decimal("-2.5")
    ).directional_score == Decimal(-1)


def test_a_constant_usd_series_has_zero_sigma_and_zero_flips() -> None:
    result = measure(days("0.3", "0.3", "0.3"), CONFIG)

    assert result.sigma == Decimal(0)
    assert all(count == 0 for count in flips_for(result, "USDJPY").values())


def test_only_usd_pairs_are_measured() -> None:
    assert {spec.symbol for spec in USD_PAIR_SPECS} == {"USDJPY", "GBPUSD", "EURUSD"}


def test_a_missing_leg_is_counted_not_projected() -> None:
    # GBP の観測が無い日は GBPUSD を射影できない（欠測 ≠ 中立、ADR-022）。
    result = measure(days("0.05", "-0.15"), CONFIG)

    gbpusd = next(pair for pair in result.pairs if pair.symbol == "GBPUSD")
    assert gbpusd.days == 0
    assert gbpusd.missing_leg_days == 2
    assert gbpusd.median_abs is None


def test_a_zero_sign_baseline_day_is_not_flippable() -> None:
    # USD = JPY = 0 の日: 基線に方向が無いので flip の母数から外れる。
    result = measure(days("0", "0.2", "-0.2"), CONFIG)

    usdjpy = next(pair for pair in result.pairs if pair.symbol == "USDJPY")
    assert usdjpy.zero_sign_days == 1
    assert usdjpy.days == 3


def test_the_report_is_deterministic() -> None:
    snapshots = days("0.05", "-0.15", "0.4")

    first = render(measure(snapshots, CONFIG))
    second = render(measure(snapshots, CONFIG))

    assert first == second
    assert "sigma(USD directional_score)" in first


def test_every_designed_step_is_measured() -> None:
    assert set(PERTURBATION_STEPS) == {
        Decimal("-1.0"),
        Decimal("-0.5"),
        Decimal("-0.25"),
        Decimal("0.25"),
        Decimal("0.5"),
        Decimal("1.0"),
    }
