"""金利差イベントスタディの B′ 固有の組み立て。

窓の計測・thin・bootstrap は E′（test_policy_event_study）で試験済みの部品を
そのまま使うので、ここでは PIT 整列・ΔD・グループ分け・報告の組み立てだけを
見る。価格・金利はすべて架空。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading.backtest.policy_event_study import Observation, divergence_slope
from trading.backtest.rate_differential_study import (
    FLAT,
    LOOKBACK,
    NARROWING,
    WIDENING,
    build_observations,
    classify_delta,
    deltas,
    differential,
    quintiles,
    report,
    visible_levels,
)
from trading.domain.economic import EconomicObservation
from trading.domain.market import Bar

T0 = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)


def bar(index: int, close: str, *, day_offset: int = 0) -> Bar:
    start = T0 + timedelta(days=index + day_offset)
    return Bar(
        symbol="USDJPY",
        timeframe="1d",
        start=start,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        known_at=start + timedelta(days=1),
    )


def vintage(period: str, value: str, known_at: datetime) -> EconomicObservation:
    return EconomicObservation(
        observation_id=uuid4(),
        series="test_series",
        observation_period=period,
        value=Decimal(value),
        unit="percent",
        source="TEST",
        retrieved_at=known_at,
        known_at=known_at,
    )


def observation(index: int, delta: float, ret: float) -> Observation:
    return Observation(
        at=T0 + timedelta(days=index),
        entry_index=index,
        group=classify_delta(delta),
        divergence=delta,
        intervention=False,
        returns={5: ret},
        adverse={5: abs(ret)},
        favorable={5: -abs(ret)},
    )


def test_a_vintage_known_after_the_instant_is_not_visible() -> None:
    # JP2Y の翌営業日公表: バー t の close 時点では t の値はまだ known でない。
    instants = [T0, T0 + timedelta(days=1)]
    vintages = [vintage("2026-04-30", "0.75", T0 + timedelta(hours=12))]

    assert visible_levels(vintages, instants) == [None, 0.75]


def test_a_revision_overwrites_but_a_late_vintage_of_an_older_day_does_not() -> None:
    vintages = [
        vintage("2026-04-30", "0.75", T0),
        # 同じ基準日の改定は水準を動かす。
        vintage("2026-04-30", "0.80", T0 + timedelta(hours=1)),
        # 遅れて届いた「より古い基準日」の vintage は最新水準ではない。
        vintage("2026-04-29", "9.99", T0 + timedelta(hours=2)),
    ]

    assert visible_levels(vintages, [T0 + timedelta(hours=3)]) == [0.80]


def test_the_differential_needs_both_legs() -> None:
    assert differential([4.0, 4.1, None], [0.7, None, 0.8]) == [3.3, None, None]


def test_delta_needs_both_endpoints() -> None:
    bars = [bar(i, "150") for i in range(LOOKBACK + 2)]
    series: list[float | None] = [None] + [3.0] * (LOOKBACK - 1) + [3.2, 3.5]

    spans = deltas(bars, series)

    # index LOOKBACK は 20 本前が None なので測れない。その次は測れる。
    assert spans[LOOKBACK] is None
    assert spans[LOOKBACK + 1] == 3.5 - 3.0


def test_a_lookback_window_crossing_a_hole_is_not_a_change() -> None:
    # 前半と後半の間に 10 日の穴。跨ぐ窓の「20 本」は時間では 20 日でない。
    bars = [bar(i, "150") for i in range(10)] + [
        bar(i, "150", day_offset=10) for i in range(10, LOOKBACK + 5)
    ]
    series: list[float | None] = [3.0] * len(bars)

    spans = deltas(bars, series)

    assert spans[LOOKBACK] is None
    assert spans[LOOKBACK + 1] is None


def test_the_sign_groups_partition_by_delta() -> None:
    assert classify_delta(0.01) == WIDENING
    assert classify_delta(-0.01) == NARROWING
    assert classify_delta(0.0) == FLAT


def test_quintiles_order_by_delta_with_q1_most_narrowing() -> None:
    kept = [observation(i * 5, delta, 0.0) for i, delta in enumerate((0.3, -0.2, 0.1, -0.4, 0.0))]

    buckets = quintiles(kept)

    assert [label for label, _ in buckets] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert [b[0].divergence for _, b in buckets] == [-0.4, -0.2, 0.0, 0.1, 0.3]


def test_an_observation_carries_every_horizon_the_series_reaches() -> None:
    bars = [bar(i, str(150 + i * 0.1)) for i in range(LOOKBACK + 8)]
    series: list[float | None] = [3.0] * LOOKBACK + [3.5] * 8
    instants = [b.close_time for b in bars]

    observations = build_observations(bars, deltas(bars, series), instants)

    # LOOKBACK 以降の各バーが1観測。5 日先までは届くが 10 日先は届かない。
    first = observations[0]
    assert first.entry_index == LOOKBACK
    assert first.group == WIDENING
    assert first.divergence == 0.5
    assert set(first.returns) == {5}


def test_the_slope_reads_positive_when_narrowing_precedes_a_lower_rate() -> None:
    # 収束テーゼの向き: ΔD が小さいほどリターンが低い並びは正の傾きになる
    # （E′ のスコア divergence とは逆符号の規約であることの記録）。
    kept = [
        observation(i * 5, delta, ret)
        for i, (delta, ret) in enumerate(
            ((-0.4, -0.02), (-0.2, -0.01), (0.1, 0.005), (0.3, 0.015))
        )
    ]

    assert divergence_slope(kept, 5) > 0


def test_the_report_reads_thinned_groups_and_a_reference_slope() -> None:
    # 7 本ごとに向きが変わるドリフトを金利水準に積み、ΔD が正負に振れる
    # 合成系列（周期が LOOKBACK の約数だと ΔD が恒等的に 0 になる）。
    bars = []
    price = 150.0
    rate = 3.0
    series: list[float | None] = []
    for i in range(LOOKBACK + 30):
        drift = -0.4 if (i // 7) % 2 else 0.4
        price += drift
        rate += 0.02 if drift > 0 else -0.02
        bars.append(bar(i, f"{price:.3f}"))
        series.append(rate)
    instants = [b.close_time for b in bars]

    observations = build_observations(bars, deltas(bars, series), instants)
    text = report(observations, bars)

    assert "slope (thinned):" in text
    assert "slope (all windows):" in text
    assert WIDENING in text and NARROWING in text
    assert "unconditional" in text
    assert "Q1" in text and "Q5" in text
