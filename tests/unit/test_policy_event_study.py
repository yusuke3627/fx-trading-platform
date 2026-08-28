"""The policy event study's measurement rules.

All prices and decisions here are fabricated.
"""
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.backtest.policy_event_study import (
    BOJ_LEG,
    BOTH_LEGS,
    FED_LEG,
    NEITHER,
    Observation,
    bootstrap_interval,
    classify,
    divergence_slope,
    entry_bar,
    summarize,
    thin,
    window_outcome,
)
from trading.domain.market import Bar

T0 = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
ANCHOR = timedelta(hours=7)


def bar(index: int, close: str, high: str | None = None, low: str | None = None) -> Bar:
    start = T0 + timedelta(days=index)
    return Bar(
        symbol="USDJPY",
        timeframe="1d",
        start=start,
        open=Decimal(close),
        high=Decimal(high or close),
        low=Decimal(low or close),
        close=Decimal(close),
        known_at=start + timedelta(days=1),
    )


def observation(index: int, group: str, ret: float, divergence: float = 0.0) -> Observation:
    return Observation(
        at=T0 + timedelta(days=index),
        entry_index=index,
        group=group,
        divergence=divergence,
        intervention=False,
        returns={5: ret},
        adverse={5: abs(ret)},
        favorable={5: -abs(ret)},
    )


def test_the_groups_partition_the_states():
    # Overlapping groups cannot tell one leg's contribution from the pair's.
    assert classify(1.0, -1.0) == BOTH_LEGS
    assert classify(1.0, 0.0) == BOJ_LEG
    assert classify(0.0, -1.0) == FED_LEG
    assert classify(0.0, 0.0) == NEITHER
    # A zero score is not hawkish: a hold with no dissent belongs outside the
    # leg, which is what the strict comparison encodes.
    assert classify(0.0, 1.0) == NEITHER


def test_entry_is_the_first_close_the_news_was_already_public_for():
    # Bars carry broker labels and the decision a real UTC instant, so the
    # two only line up through the replay's reconstruction: under the summer
    # anchor the candle labelled 2026-05-02 closes at 2026-05-02T21:00Z.
    bars = [bar(i, "150.00") for i in range(4)]

    # Announced while that candle was still open — its close is still the
    # earliest price the news could have been acted on.
    assert entry_bar(bars, datetime(2026, 5, 2, 12, 0, tzinfo=UTC), ANCHOR) == 1
    # Announced after it closed: that close came too early to act on.
    assert entry_bar(bars, datetime(2026, 5, 2, 22, 0, tzinfo=UTC), ANCHOR) == 2


def test_a_decision_after_the_series_ends_has_no_entry():
    bars = [bar(i, "150.00") for i in range(3)]

    assert entry_bar(bars, T0 + timedelta(days=30), ANCHOR) is None


def test_the_window_measures_a_short_and_its_excursions():
    bars = [
        bar(0, "150.00"),
        bar(1, "151.00", high="152.00", low="149.00"),
        bar(2, "148.50", high="149.50", low="147.00"),
    ]

    ret, adverse, favorable = window_outcome(bars, 0, 2)

    # Yen appreciation is a negative return: the direction the thesis claims.
    assert ret < 0
    # The window went against a short before it went for it, which a mean
    # return alone would hide.
    assert adverse > 0
    assert favorable < 0


def test_a_window_running_past_the_series_is_not_measured():
    bars = [bar(i, "150.00") for i in range(3)]

    assert window_outcome(bars, 1, 5) is None


def test_thinning_keeps_windows_that_do_not_share_days():
    # Two decisions three weeks apart share most of a twenty-day window;
    # averaging both counts the same price move twice.
    crowded = [
        observation(0, BOTH_LEGS, -0.01),
        observation(2, BOTH_LEGS, -0.01),
        observation(6, BOTH_LEGS, -0.01),
        observation(9, BOTH_LEGS, -0.01),
    ]

    assert [o.entry_index for o in thin(crowded, 5)] == [0, 6]


def test_summary_counts_only_the_windows_that_ended_lower():
    stats = summarize(
        [
            observation(0, BOTH_LEGS, -0.02),
            observation(10, BOTH_LEGS, -0.01),
            observation(20, BOTH_LEGS, 0.03),
        ],
        5,
        seed=1,
    )

    assert stats.count == 3
    assert stats.hit_rate == 2 / 3
    assert stats.median == -0.01
    assert stats.low < stats.mean < stats.high


def test_an_empty_group_reports_no_observations():
    assert summarize([], 5, seed=1).count == 0


def test_the_interval_is_reproducible_and_needs_more_than_one_observation():
    values = [-0.02, -0.01, 0.03, 0.01]

    assert bootstrap_interval(values, seed=7) == bootstrap_interval(values, seed=7)
    assert bootstrap_interval(values, seed=7) != bootstrap_interval(values, seed=8)
    # One observation spans nothing: NaN rather than a zero-width interval
    # that would read as certainty.
    low, high = bootstrap_interval([0.01], seed=7)
    assert math.isnan(low) and math.isnan(high)


def test_the_slope_reads_negative_when_wider_divergence_precedes_a_lower_rate():
    observations = [
        observation(0, BOTH_LEGS, 0.02, divergence=0.0),
        observation(10, BOTH_LEGS, 0.00, divergence=1.0),
        observation(20, BOTH_LEGS, -0.02, divergence=2.0),
    ]

    assert divergence_slope(observations, 5) < 0


def test_the_slope_is_undefined_when_every_decision_carries_the_same_divergence():
    observations = [
        observation(0, BOTH_LEGS, 0.02, divergence=1.0),
        observation(10, BOTH_LEGS, -0.02, divergence=1.0),
    ]

    # NaN rather than a fabricated zero slope.
    assert math.isnan(divergence_slope(observations, 5))
