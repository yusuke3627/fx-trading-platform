"""特徴量スクリーンの過去窓、統計、判定順序、CLI 境界を検証する。"""
from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from statistics import NormalDist

import pytest
from pydantic import ValidationError

from tests.support import usdjpy_spec
from trading.backtest.feature_screen import (
    Period,
    Plan,
    Sample,
    critical_value,
    feature_values,
    ic_interval,
    load_bars,
    main,
    normalize,
    run,
    select_samples,
    spearman,
    split_segments,
    summarize,
    verdict,
)
from trading.backtest.research import BAR_CSV_HEADER, write_bar
from trading.domain.market import TIMEFRAME_SECONDS, Bar
from trading.indicators import DEFAULT_BAR_COUNT

START = datetime(2026, 1, 1, tzinfo=UTC)
FEATURES = (
    {"id": "range", "kind": "range_position", "lookback": 2},
    {"id": "slope", "kind": "ema_slope_atr", "ema_period": 2, "slope_lookback": 2},
    {"id": "distance", "kind": "distance_from_ema_atr", "ema_period": 2},
    {"id": "momentum", "kind": "momentum_atr", "lookback": 2},
)
NEW_FEATURES = (
    {"id": "squeeze", "kind": "squeeze_range_position", "lookback": 2,
     "width_window": 3, "max_width_share": 0.5},
    {"id": "slot", "kind": "same_slot_mean_return", "occurrences": 2},
)


def plan(**overrides) -> Plan:
    values = {
        "schema_version": "feature_screen_v1",
        "population_description": "固定 seed の架空価格による単体検証",
        "basis": "replay_bars", "instrument": usdjpy_spec(symbol="TEST_PAIR"),
        "timeframe": "1h", "explore": {"start": START, "end": "2026-04-01T00:00:00Z"},
        "confirm": {"start": "2026-04-01T00:00:00Z", "end": "2026-07-01T00:00:00Z"},
        "max_gap_hours": 72, "indicator_bars": 20, "atr_period": 5,
        "normalization_window": 30, "entry_z": 0.5,
        "features": [{"id": "momentum", "kind": "momentum_atr", "lookback": 1}],
        "horizons": [1, 4], "round_trip_cost_pips": "0.005",
        "cost_rationale": "単体テストの架空コスト。実際の執行コストではない",
        "quantiles": 5, "min_samples": 30, "min_month_samples": 5,
        "staircase_min": 0.8, "month_sign_min": 0.75,
    }
    values.update(overrides)
    return Plan.model_validate(values)


def make_bar(start: datetime, close: Decimal, spread: Decimal = Decimal("0.025")) -> Bar:
    return Bar(symbol="TEST_PAIR", timeframe="1h", start=start, open=close,
               high=close + spread, low=close - spread, close=close,
               tick_volume=5, known_at=start + timedelta(hours=1))


def synthetic_bars(*, count: int = 4344, rho: float = 0.85, seed: int = 713) -> list[Bar]:
    rng = random.Random(seed)
    close = Decimal(100)
    previous = 0.0
    bars = []
    for i in range(count):
        previous = rho * previous + rng.gauss(0, 0.5)
        close += Decimal(str(previous)) * Decimal("0.01")
        bars.append(make_bar(START + timedelta(hours=i), close))
    return bars


def write_bars(path: Path, bars: list[Bar]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as out:
        out.write(BAR_CSV_HEADER)
        for bar in bars:
            write_bar(bar, out)
    return path


def cli_inputs(tmp_path: Path, p: Plan, bars: list[Bar]) -> tuple[Path, Path]:
    plan_path = tmp_path / "input-plan.json"
    plan_path.write_text(p.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return plan_path, write_bars(tmp_path / "bars.csv", bars)


def cli_args(plan_path: Path, bars_path: Path, output: Path) -> list[str]:
    return ["--plan", str(plan_path), "--bars", str(bars_path), "--output-dir", str(output)]


def test_spearman_average_ranks_match_hand_calculation():
    # 順位 x=[1,2.5,2.5,4], y=[1,2,3.5,3.5]。共分散の分子 3.75、平方和は各 4.5。
    assert spearman([1, 2, 2, 4], [1, 2, 3, 3]) == pytest.approx(5 / 6)
    assert spearman([1, 2, 2, 4], [-1, -2, -3, -3]) == pytest.approx(-5 / 6)
    assert spearman([1, 1], [1, 2]) is None
    assert spearman([], []) is None


def test_features_use_existing_atr_ema_and_exclude_current_range():
    p = plan(features=FEATURES, indicator_bars=4, atr_period=2)
    bars = [make_bar(START + timedelta(hours=i), Decimal(close), Decimal("0.5"))
            for i, close in enumerate((10, 12, 11, 14, 15))]
    values = feature_values(bars, p, [(0, 5)])
    assert all(v[:3] == [None] * 3 for v in values.values())
    assert values["range"][-2] == pytest.approx(2.5)
    assert values["slope"][-2] == pytest.approx(2 / 2.75)
    assert values["distance"][-2] == pytest.approx(1 / 2.75)
    assert values["momentum"][-2] == pytest.approx(2 / 2.75)
    # 次の足では末尾 4 本で再初期化する。全履歴を流用した EMA/ATR と区別する。
    assert values["range"][-1] == pytest.approx(1.25)
    assert values["slope"][-1] == pytest.approx(13 / 9)
    assert values["distance"][-1] == pytest.approx(11 / 36)
    assert values["momentum"][-1] == pytest.approx(2)


def test_pit_future_rewrite_does_not_change_earlier_features_or_z():
    p = plan(features=FEATURES + NEW_FEATURES)
    bars = synthetic_bars(count=240)
    k = 180
    changed = bars[:k] + [b.model_copy(update={
        "open": b.open * i, "high": b.high * i, "low": b.low * i, "close": b.close * i,
    }) for i, b in enumerate(bars[k:], start=2)]
    segments = [(0, len(bars))]
    original = feature_values(bars, p, segments)
    rewritten = feature_values(changed, p, segments)
    for feature in p.features:
        before, after = original[feature.id], rewritten[feature.id]
        assert before[:k] == after[:k]
        assert before[k:] != after[k:]
        z_before = normalize(before, p.normalization_window, segments)
        z_after = normalize(after, p.normalization_window, segments)
        assert any(z is not None for z in z_before[:k])
        assert z_before[:k] == z_after[:k]


def test_squeeze_uses_previous_ranges_and_filters_wide_ranges():
    p = plan(features=[NEW_FEATURES[0]], indicator_bars=6)
    bars = [make_bar(START + timedelta(hours=i), Decimal(close), Decimal(spread))
            for i, (close, spread) in enumerate([
                (100, 5), (100, 4), (100, 3), (100, 1), (100, 1), (104, 4), (100, 0),
            ])]
    values = feature_values(bars, p, [(0, len(bars))])["squeeze"]
    # W_2..W_5 = 10, 8, 6, 2。足 5 の直前レンジは [99,101]。
    assert values[:5] == [None] * 5
    assert values[5] == pytest.approx(4)
    # W_6 = 9 は直前の幅 8, 6, 2 よりすべて大きい。
    assert values[6] is None


@pytest.mark.parametrize("threshold,expected", [(0.25, 2), (0.249, None)])
def test_squeeze_tied_widths_are_not_smaller_and_threshold_is_inclusive(threshold, expected):
    p = plan(features=[NEW_FEATURES[0] | {
        "lookback": 1, "width_window": 4, "max_width_share": threshold,
    }], indicator_bars=6)
    bars = [make_bar(START + timedelta(hours=i), Decimal(100), Decimal(spread))
            for i, spread in enumerate([1, 2, 3, 2, 2])]
    bars.append(make_bar(START + timedelta(hours=5), Decimal(104)))
    # 直前の幅 [2,4,6,4] のうち W_5=4 より小さいのは 1/4。
    assert feature_values(bars, p, [(0, 6)])["squeeze"][-1] == expected


def test_squeeze_zero_width_is_missing_without_atr_requirement():
    p = plan(features=[NEW_FEATURES[0] | {"lookback": 1, "width_window": 1}],
             indicator_bars=3, atr_period=100)
    bars = [make_bar(START + timedelta(hours=i), Decimal(100), Decimal(0)) for i in range(6)]
    assert feature_values(bars, p, [(0, 6)])["squeeze"] == [None] * 6
    bars[1] = make_bar(bars[1].start, Decimal(100), Decimal(1))
    bars[2] = make_bar(bars[2].start, Decimal(100), Decimal("0.5"))
    bars[3] = make_bar(bars[3].start, Decimal(102), Decimal(0))
    assert feature_values(bars, p, [(0, 6)])["squeeze"][3] == pytest.approx(4)


@pytest.mark.parametrize("timeframe", ["15m", "1h", "4h"])
def test_same_slot_uses_latest_occurrences_before_warmup_and_needs_no_next_bar(timeframe):
    step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    per_day = 86400 // TIMEFRAME_SECONDS[timeframe]
    p = plan(features=[NEW_FEATURES[1]], timeframe=timeframe,
             indicator_bars=2 * per_day + 1,
             instrument=usdjpy_spec(symbol="TEST_PAIR", pip_size=Decimal("0.0001")))
    close = Decimal("100.00000000000001")
    bars = []
    for i in range(3 * per_day + 1):
        change = {1: 1, per_day + 1: 3, 2 * per_day + 1: 7}.get(i, 90)
        close += Decimal(change) * p.instrument.pip_size
        bars.append(make_bar(START + i * step, close).model_copy(update={
            "timeframe": timeframe, "known_at": START + (i + 1) * step,
        }))
    values = feature_values(bars, p, [(0, len(bars))])["slot"]
    assert values[:2 * per_day] == [None] * (2 * per_day)
    assert values[2 * per_day] == pytest.approx(2)
    assert values[-1] == pytest.approx(5)
    scarce = plan(features=[NEW_FEATURES[1] | {"occurrences": 4}],
                  timeframe=timeframe, indicator_bars=1)
    assert all(v is None for v in feature_values(bars, scarce, [(0, len(bars))])["slot"])


def test_same_slot_excludes_missing_bars_and_weekend_returns_within_segment():
    p = plan(features=[NEW_FEATURES[1]], indicator_bars=1)
    hours = [0, 23, 24, 46, 48, 71, 72, 120, 143, 144, 167]
    close = Decimal(100)
    bars = []
    for hour in hours:
        close += Decimal({24: 2, 72: 6, 144: 10}.get(hour, 1000)) * p.instrument.pip_size
        bars.append(make_bar(START + timedelta(hours=hour), close))
    segments = split_segments(bars, p.max_gap_hours)
    assert segments == [(0, len(bars))]
    values = feature_values(bars, p, segments)["slot"]
    # 48 時間目と 120 時間目の差は欠損をまたぐので、0 時の履歴に入らない。
    assert values[hours.index(71)] is None
    assert values[hours.index(143)] == pytest.approx(4)
    assert values[hours.index(167)] == pytest.approx(8)


def test_same_slot_finite_mean_does_not_overflow_during_summation():
    p = plan(features=[NEW_FEATURES[1]], indicator_bars=1,
             instrument=usdjpy_spec(symbol="TEST_PAIR", pip_size=Decimal("0.1")))
    bars = [make_bar(START + timedelta(hours=i),
                     Decimal("1e307") if i in (1, 25) else Decimal(1)) for i in range(49)]
    assert feature_values(bars, p, [(0, len(bars))])["slot"][-1] == pytest.approx(1e308)


@pytest.mark.parametrize("kind", ["squeeze", "slot"])
def test_cli_rejects_nonfinite_new_feature_values(tmp_path, capsys, kind):
    if kind == "squeeze":
        p = plan(features=[NEW_FEATURES[0] | {"lookback": 1, "width_window": 1}],
                 indicator_bars=3)
        bars = [
            make_bar(START, Decimal("3e-308"), Decimal("2e-308")),
            make_bar(START + timedelta(hours=1), Decimal("2e-308"), Decimal("1e-308")),
            make_bar(START + timedelta(hours=2), Decimal(100)),
        ]
    else:
        p = plan(features=[NEW_FEATURES[1] | {"occurrences": 1}], indicator_bars=1,
                 instrument=usdjpy_spec(symbol="TEST_PAIR", pip_size=Decimal("1e-308")))
        bars = [make_bar(START + timedelta(hours=i), Decimal(10) if i else Decimal(1))
                for i in range(25)]
    plan_path, bars_path = cli_inputs(tmp_path, p, bars)
    output = tmp_path / "output"
    assert main(cli_args(plan_path, bars_path, output)) == 2
    assert "特徴量が数値範囲を超えています" in capsys.readouterr().err
    assert not output.exists()


def test_new_features_reset_history_at_long_gap():
    p = plan(features=NEW_FEATURES, indicator_bars=6)
    bars = synthetic_bars(count=300)
    bars[150:] = [b.model_copy(update={"start": b.start + timedelta(days=5)}) for b in bars[150:]]
    segments = split_segments(bars, p.max_gap_hours)
    assert segments == [(0, 150), (150, 300)]
    values = feature_values(bars, p, segments)
    restarted = feature_values(bars[150:], p, [(0, 150)])
    for feature in p.features:
        assert any(v is not None for v in values[feature.id][:150])
        assert values[feature.id][150:] == restarted[feature.id]
    assert values["squeeze"][150:155] == [None] * 5
    assert any(v is not None for v in values["squeeze"][155:])
    assert values["slot"][150:198] == [None] * 48
    assert values["slot"][198] is not None


def test_z_uses_only_previous_valid_values_and_resets_at_segment():
    values = list(range(20)) + [None, 100] + list(range(20)) + [20]
    scores = normalize(values, 20, [(0, 22), (22, 43)])
    assert scores[:21] == [None] * 21
    assert scores[21] == pytest.approx((100 - 9.5) / math.sqrt(33.25))
    assert scores[22:42] == [None] * 20
    assert scores[42] == pytest.approx((20 - 9.5) / math.sqrt(33.25))
    assert normalize([1] * 21, 20, [(0, 21)]) == [None] * 21


def test_flat_range_and_atr_are_missing():
    p = plan(features=FEATURES)
    bars = [make_bar(START + timedelta(hours=i), Decimal(100), Decimal(0)) for i in range(60)]
    assert all(all(v is None for v in series)
               for series in feature_values(bars, p, [(0, 60)]).values())


def test_long_gap_resets_both_indicator_and_normalization_windows():
    p = plan(features=FEATURES)
    bars = synthetic_bars(count=150)
    bars[75:] = [b.model_copy(update={"start": b.start + timedelta(days=5)}) for b in bars[75:]]
    segments = split_segments(bars, p.max_gap_hours)
    assert segments == [(0, 75), (75, 150)]
    for values in feature_values(bars, p, segments).values():
        assert values[75:94] == [None] * 19
        scores = normalize(values, p.normalization_window, segments)
        assert scores[75:124] == [None] * 49
        assert scores[124] is not None


def test_nonoverlap_skips_weekend_gaps_and_respects_period_boundaries():
    bars = synthetic_bars(count=75)
    bars[25:50] = [b.model_copy(update={"start": b.start + timedelta(hours=48)}) for b in bars[25:50]]
    bars[50:] = [b.model_copy(update={"start": b.start + timedelta(hours=168)}) for b in bars[50:]]
    p = plan()
    segments = split_segments(bars, p.max_gap_hours)
    assert segments == [(0, 50), (50, 75)]
    boundary = bars[40].close_time
    explore = Period(start=bars[5].close_time, end=boundary)
    confirm = Period(start=boundary, end=bars[-1].close_time)
    scores = [1.0] * len(bars)
    first = select_samples(bars, scores, 4, explore, p, segments)
    second = select_samples(bars, scores, 4, confirm, p, segments)
    assert first[0].index == 5
    assert second[0].index == 40
    for samples, period in ((first, explore), (second, confirm)):
        assert samples
        assert all(b.index - a.index >= 4 for a, b in pairwise(samples))
        for sample in samples:
            t = sample.index
            assert bars[t + 4].start - bars[t].start == timedelta(hours=4)
            assert period.start <= sample.close_time
            assert bars[t + 4].close_time <= period.end
            assert any(start <= t < t + 4 < end for start, end in segments)
    # 戻り値の右端が期間末尾と等しい標本は採用する。
    exact = Period(start=bars[5].close_time, end=bars[9].close_time)
    assert [s.index for s in select_samples(bars, scores, 4, exact, p, segments)] == [5]


def test_greedy_selection_advances_only_on_valid_samples_and_uses_pip_size():
    bars = [make_bar(START + timedelta(hours=i), Decimal(100 + i)) for i in range(12)]
    p = plan(instrument=usdjpy_spec(symbol="TEST_PAIR", pip_size=Decimal("0.25")))
    samples = select_samples(bars, [None, None] + [1.0] * 10, 3, p.explore, p, [(0, 12)])
    assert [s.index for s in samples] == [2, 5, 8]
    assert [s.return_pips for s in samples] == [12, 12, 12]


def test_signal_candidates_confirm_noise_is_not_detected_and_simulated_is_never_adopted(tmp_path):
    p = plan()
    path = write_bars(tmp_path / "signal.csv", synthetic_bars())
    report = run(p, path)
    assert report["family_size"] == {"explore": 2, "confirm": 2}
    for cell in report["cells"]:
        assert cell["explore"]["ic"] > 0
        assert cell["explore"]["status"] == "candidate"
        assert cell["confirm"]["status"] == "confirmed"
        assert cell["explore"]["month_count"] == 3
    noise = run(p, write_bars(tmp_path / "noise.csv", synthetic_bars(rho=0)))
    assert all(c["explore"]["status"] == "not_detected" for c in noise["cells"])
    assert all(c["confirm"]["status"] == "not_evaluated" for c in noise["cells"])
    assert noise["family_size"]["confirm"] == 0
    assert noise["z_crit"]["confirm"] is None
    mixed = run(plan(features=[p.features[0].model_dump(), {
        "id": "flat", "kind": "distance_from_ema_atr", "ema_period": 1,
    }]), path)
    assert mixed["family_size"] == {"explore": 4, "confirm": 2}
    assert mixed["z_crit"]["explore"] > mixed["z_crit"]["confirm"]
    assert [c["confirm"]["status"] for c in mixed["cells"]] == [
        "confirmed", "confirmed", "not_evaluated", "not_evaluated",
    ]
    simulated = run(plan(basis="simulated"), path)
    for cell in simulated["cells"]:
        assert cell["explore"]["status"] == cell["confirm"]["status"] == "synthetic_only"
        assert cell["confirm"]["ic"] is not None
        assert cell["confirm"]["quantiles"]
    assert simulated["profitability_established"] is False


def signal_samples(n: int = 120, *, direction: int = 1) -> list[Sample]:
    rng = random.Random(619)
    return [Sample(i, START + timedelta(days=i), z, direction * (2 * z + rng.gauss(0, 0.1)))
            for i in range(n) for z in [rng.uniform(-2, 2)]]


def test_cost_formula_and_below_cost_preserve_negative_signal_direction():
    p = plan(round_trip_cost_pips="10")
    stats = summarize(signal_samples(direction=-1), p, critical_value(p.confidence, 1))
    assert stats["ic"] < 0
    assert stats["entry_mean_pips"] > 0
    assert stats["entry_net_pips"] == pytest.approx(stats["entry_mean_pips"] - 10)
    assert stats["break_even_ic"] == pytest.approx(10 / (stats["sigma_pips"] * stats["mean_abs_z"]))
    assert verdict(stats, p)[0] == "below_cost"
    cheap = plan()
    stats = summarize(signal_samples(direction=-1), cheap, critical_value(cheap.confidence, 1))
    assert verdict(stats, cheap)[0] == "candidate"


@pytest.mark.parametrize("means", [(1000, 4, 3, 2, 1), (0, 4, 2, 3, 1)])
def test_inverted_or_nonmonotonic_quantile_means_are_unstable(means):
    # 各分位の大半は Z とともに上がるが、少数の外れ値で平均の階段が崩れる。
    samples = []
    for q, target_mean in enumerate(means):
        returns = [float(q)] * 19 + [target_mean * 20 - q * 19]
        samples.extend(Sample(q * 20 + i, START + timedelta(days=q * 20 + i),
                              q + i / 100, r) for i, r in enumerate(returns))
    p = plan(month_sign_min=0, entry_z=0.1)
    stats = summarize(samples, p, critical_value(p.confidence, 1))
    assert stats["ic"] > 0
    assert stats["ic_interval"][0] > 0
    assert stats["staircase"] < p.staircase_min
    assert verdict(stats, p)[0] == "unstable"
    assert "staircase_min" in verdict(stats, p)[1]


def test_bonferroni_and_fisher_interval_match_fixed_formula():
    one, many = critical_value(0.9, 1), critical_value(0.9, 12)
    assert many > one
    assert many == pytest.approx(NormalDist().inv_cdf(1 - 0.1 / 24))
    ci = ic_interval(0.4, 103, one)
    assert ci == pytest.approx([
        math.tanh(math.atanh(0.4) - one * 1.06 / 10),
        math.tanh(math.atanh(0.4) + one * 1.06 / 10),
    ])
    assert ic_interval(0.4, 103, many)[0] < ci[0]
    assert ic_interval(1, 30, one) == [1, 1]
    assert ic_interval(-1, 30, one) == [-1, -1]
    assert ic_interval(0.4, 3, one) is None


def test_quantile_ties_use_time_and_position_not_value_thresholds():
    p = plan(quantiles=3)
    samples = [Sample(i, START + timedelta(hours=i), 1, float(i)) for i in reversed(range(7))]
    stats = summarize(samples, p, 2)
    assert [q["n"] for q in stats["quantiles"]] == [3, 2, 2]
    assert [q["mean_return_pips"] for q in stats["quantiles"]] == [1, 3.5, 5.5]
    assert stats["ic"] is None


def test_monthly_statistics_cumulative_mean_t_and_minimum_sample_filter():
    samples = []
    for month, returns in enumerate(([0, 1, 2, 3, 4], [0, 1, 2, 4, 3], [4, 3, 2, 1, 0],
                                     [0, 1, 2, 3]), start=1):
        samples.extend(Sample(len(samples) + i, datetime(2026, month, i + 1, tzinfo=UTC),
                              float(i), float(value)) for i, value in enumerate(returns))
    stats = summarize(samples, plan(), 2)
    assert [m["ic"] for m in stats["months"]] == pytest.approx([1, 0.9, -1])
    assert [m["cumulative_ic"] for m in stats["months"]] == pytest.approx([1, 1.9, 0.9])
    assert stats["month_count"] == 3
    assert stats["month_ic_mean"] == pytest.approx(0.3)
    assert stats["month_ic_t"] == pytest.approx(0.3 / math.sqrt(1.27 / 3))
    assert stats["month_sign_share"] == pytest.approx(2 / 3)


@pytest.mark.parametrize("changes,expected", [
    ({"n": 29}, "insufficient"),
    ({"entry_n": 0}, "insufficient"),
    ({"ic_interval": [0, 0.9]}, "not_detected"),
    ({"break_even_ic": 2}, "below_cost"),
    ({"month_count": 0}, "unstable"),
    ({"month_sign_share": 0.1}, "unstable"),
    ({"staircase": None}, "unstable"),
])
def test_explore_verdict_order_and_missing_statistics(changes, expected):
    p = plan()
    stats = summarize(signal_samples(), p, 2)
    assert verdict(stats, p)[0] == "candidate"
    assert verdict(stats | changes, p)[0] == expected
    assert verdict(stats | changes, plan(basis="simulated"))[0] == "synthetic_only"


@pytest.mark.parametrize("changes,expected", [
    ({"n": 29}, "insufficient"),
    ({"entry_n": 0}, "insufficient"),
    ({"ic": -0.9, "ic_interval": [-0.99, -0.8]}, "not_confirmed"),
    ({"ic_interval": [-0.1, 0.9]}, "not_confirmed"),
    ({"break_even_ic": 2}, "below_cost"),
    ({"month_count": 0, "staircase": -1, "month_sign_share": 0}, "confirmed"),
])
def test_confirm_uses_candidates_and_only_confirmation_rules(changes, expected):
    p = plan()
    stats = summarize(signal_samples(), p, 2)
    explore = stats | {"status": "candidate"}
    assert verdict(stats | changes, p, explore=explore)[0] == expected
    assert verdict(stats | changes, p, explore=explore | {"status": "unstable"})[0] == "not_evaluated"


@pytest.mark.parametrize("overrides", [
    {"timeframe": "2h"}, {"horizons": []}, {"horizons": [2, 1]}, {"horizons": [1, 1]},
    {"horizons": [0]}, {"horizons": [True]}, {"features": []},
    {"features": [FEATURES[0], FEATURES[0]]}, {"indicator_bars": 0},
    {"normalization_window": 19}, {"min_samples": 29}, {"min_month_samples": 4},
    {"quantiles": 11}, {"round_trip_cost_pips": "0"}, {"round_trip_cost_pips": "NaN"},
    {"max_gap_hours": float("inf")}, {"entry_z": 0}, {"confidence": 1},
    {"population_description": " "}, {"cost_rationale": ""}, {"unplanned": True},
    {"explore": {"start": "2026-01-01", "end": "2026-04-01T00:00:00Z"}},
    {"explore": {"start": "2026-01-01T00:00:00Z", "end": "2026-05-01T00:00:00Z"}},
    {"confirm": {"start": "2026-04-01T00:00:00Z", "end": "2026-04-01T00:00:00Z"}},
    {"features": [{"id": "range", "kind": "range_position", "lookback": 2, "extra": 1}]},
    {"instrument": usdjpy_spec(pip_size=Decimal(0))},
])
def test_plan_rejects_invalid_boundaries(overrides):
    with pytest.raises(ValidationError):
        plan(**overrides)


@pytest.mark.parametrize("feature,required", [
    ({"id": "r", "kind": "range_position", "lookback": 9}, 10),
    ({"id": "m", "kind": "momentum_atr", "lookback": 2}, 6),
    ({"id": "m", "kind": "momentum_atr", "lookback": 9}, 10),
    ({"id": "d", "kind": "distance_from_ema_atr", "ema_period": 8}, 8),
    ({"id": "s", "kind": "ema_slope_atr", "ema_period": 8, "slope_lookback": 4}, 12),
    (NEW_FEATURES[0], 6),
])
def test_indicator_window_matches_existing_api_minimum(feature, required):
    assert plan(features=[feature], indicator_bars=required).indicator_bars == required
    with pytest.raises(ValidationError, match="indicator_bars"):
        plan(features=[feature], indicator_bars=required - 1)


@pytest.mark.parametrize("feature,overrides", [
    (NEW_FEATURES[0], {"lookback": 0}),
    (NEW_FEATURES[0], {"lookback": True}),
    (NEW_FEATURES[0], {"width_window": 0}),
    (NEW_FEATURES[0], {"width_window": 1.5}),
    (NEW_FEATURES[0], {"max_width_share": 0}),
    (NEW_FEATURES[0], {"max_width_share": 1}),
    (NEW_FEATURES[0], {"max_width_share": float("nan")}),
    (NEW_FEATURES[0], {"max_width_share": float("inf")}),
    (NEW_FEATURES[1], {"occurrences": 0}),
    (NEW_FEATURES[1], {"occurrences": True}),
    (NEW_FEATURES[1], {"occurrences": 1.5}),
])
def test_new_feature_parameters_reject_invalid_boundaries(feature, overrides):
    with pytest.raises(ValidationError):
        plan(features=[feature | overrides])


def test_same_slot_rejects_daily_timeframe_but_squeeze_accepts_it():
    with pytest.raises(ValidationError, match="timeframe.*1 日未満"):
        plan(features=[NEW_FEATURES[1]], timeframe="1d")
    assert plan(features=[NEW_FEATURES[0]], timeframe="1d").timeframe == "1d"


def test_plan_is_frozen_and_defaults_follow_existing_indicator_window():
    data = plan().model_dump(mode="json")
    del data["indicator_bars"], data["atr_period"]
    p = Plan.model_validate(data)
    assert p.indicator_bars == DEFAULT_BAR_COUNT
    assert p.atr_period == 14
    with pytest.raises(ValidationError):
        p.confidence = 0.8


@pytest.mark.parametrize("content", [
    "start,open,high,low,close\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00,100,101,99,100,2\n",
    BAR_CSV_HEADER + "2026-01-01T00:01:00Z,100,101,99,100,2\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00.000001Z,100,101,99,100,2\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,100,99,99,100,2\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,100,101,101,100,2\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,NaN,101,99,100,2\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,Infinity,101,99,100,2\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,0,101,99,100,2\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,invalid,101,99,100,2\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,100,101,99,100,-1\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,100,101,99,100,1.5\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,100,101,99,100\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,100,101,99,100,2,extra\n",
    BAR_CSV_HEADER + "2026-01-01T00:00:00Z,100,101,99,100,2\n" * 2,
    BAR_CSV_HEADER + "2026-01-02T00:00:00Z,100,101,99,100,2\n"
    "2026-01-01T00:00:00Z,100,101,99,100,2\n",
    BAR_CSV_HEADER + '"unclosed,100,101,99,100,2\n',
])
def test_cli_rejects_malformed_csv_without_creating_output(tmp_path, capsys, content):
    plan_path, bars_path = cli_inputs(tmp_path, plan(), [])
    bars_path.write_text(content)
    output = tmp_path / "output"
    assert main(cli_args(plan_path, bars_path, output)) == 2
    assert "入力または出力エラー" in capsys.readouterr().err
    assert not output.exists()


def test_cli_rejects_invalid_plan_and_missing_file(tmp_path):
    plan_path, bars_path = cli_inputs(tmp_path, plan(), [])
    plan_path.write_text('{"schema_version": "wrong"}')
    assert main(cli_args(plan_path, bars_path, tmp_path / "bad-plan")) == 2
    plan_path.unlink()
    assert main(cli_args(plan_path, bars_path, tmp_path / "missing")) == 2


@pytest.mark.parametrize("field,value", [
    ("pip_size", "1e-10000"), ("pip_size", "1e10000"),
    ("round_trip_cost_pips", "1e-10000"), ("round_trip_cost_pips", "1e10000"),
])
def test_cli_rejects_unrepresentable_numeric_input(tmp_path, field, value):
    plan_path, bars_path = cli_inputs(tmp_path, plan(), synthetic_bars(count=80))
    data = json.loads(plan_path.read_text())
    target = data["instrument"] if field == "pip_size" else data
    target[field] = value
    plan_path.write_text(json.dumps(data))
    assert main(cli_args(plan_path, bars_path, tmp_path / "output")) == 2
    assert not (tmp_path / "output").exists()


def test_cli_rejects_overflow_when_finite_prices_are_converted_to_pips(tmp_path, capsys):
    p = plan(instrument=usdjpy_spec(symbol="TEST_PAIR", pip_size=Decimal("1e-308")))
    bars = synthetic_bars(count=80)
    bars[-1] = make_bar(bars[-1].start, Decimal(110))
    plan_path, bars_path = cli_inputs(tmp_path, p, bars)
    assert main(cli_args(plan_path, bars_path, tmp_path / "output")) == 2
    assert "pips 換算" in capsys.readouterr().err


@pytest.mark.parametrize("features", [FEATURES, NEW_FEATURES, FEATURES + NEW_FEATURES])
def test_cli_writes_exact_inputs_hashes_and_reports_and_refuses_overwrite(tmp_path, features):
    p = plan(basis="simulated", features=features)
    bars = synthetic_bars(count=250)
    plan_path, bars_path = cli_inputs(tmp_path, p, bars)
    raw_plan = plan_path.read_bytes()
    output = tmp_path / "output"
    args = cli_args(plan_path, bars_path, output)
    assert main(args) == 0
    assert {p.name for p in output.iterdir()} == {"plan.json", "report.json", "report.md"}
    assert (output / "plan.json").read_bytes() == raw_plan
    report = json.loads((output / "report.json").read_text())
    assert report["schema_version"] == "feature_screen_report_v1"
    assert report["profitability_established"] is False
    assert report["plan_sha256"] == hashlib.sha256(raw_plan).hexdigest()
    assert report["bars"]["sha256"] == hashlib.sha256(bars_path.read_bytes()).hexdigest()
    assert report["bars"]["bar_count"] == 250
    assert report["bars"]["first_start"] == str(bars[0].start)
    assert report["bars"]["last_start"] == str(bars[-1].start)
    assert report["bars"]["segments"][0]["bar_count"] == 250
    assert "git_commit" in report["git"]
    assert len(report["cells"]) == len(features) * len(p.horizons)
    assert {c["feature_id"] for c in report["cells"]} == {f["id"] for f in features}
    assert all(c["explore"]["n"] > 0 for c in report["cells"])
    assert len(report["ic_decay"]) == len(features)
    assert report["ic_decay"][0]["horizons"][0]["explore"] == report["cells"][0]["explore"]["ic"]
    markdown = (output / "report.md").read_text()
    for phrase in ("収益性・将来の再現性は示しません", "simulated", "合成データ", "探索期間", "確認期間",
                   "IC 減衰", "分位", "理由", "synthetic_only"):
        assert phrase in markdown
    previous = {f.name: f.read_bytes() for f in output.iterdir()}
    assert main(args) == 2
    assert {f.name: f.read_bytes() for f in output.iterdir()} == previous


def test_empty_input_and_constant_returns_are_not_candidates(tmp_path):
    p = plan()
    path = write_bars(tmp_path / "empty.csv", [])
    report = run(p, path)
    assert report["bars"]["segments"] == []
    assert all(c["explore"]["status"] == "insufficient" for c in report["cells"])
    stats = summarize([Sample(i, START + timedelta(days=i), float(i), 0) for i in range(60)], p, 2)
    assert stats["break_even_ic"] is None
    assert stats["ic"] is None
    assert verdict(stats, p)[0] == "not_detected"


def test_csv_uses_plan_identity_and_preserves_decimal_prices(tmp_path):
    original = synthetic_bars(count=2)
    path = write_bars(tmp_path / "bars.csv", original)
    bars, _ = load_bars(path, plan())
    assert bars == original
    assert all(isinstance(b.close, Decimal) for b in bars)
