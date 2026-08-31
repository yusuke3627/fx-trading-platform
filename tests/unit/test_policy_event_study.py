"""The policy event study's measurement rules.

All prices and decisions here are fabricated.
"""
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading.backtest.policy_event_study import (
    BOJ_LEG,
    BOTH_LEGS,
    FED_LEG,
    NEITHER,
    Observation,
    bootstrap_interval,
    classify,
    collapse_same_entry,
    current_version,
    divergence_slope,
    entry_bar,
    fold_daily,
    gaps,
    irregular_steps,
    measured_span,
    summarize,
    thin,
    window_outcome,
)
from trading.data.policy.scoring import SCORING_VERSION
from trading.domain.event import EventEnvelope
from trading.domain.market import Bar, Tick

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


def decision(version: str) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type="BOJ_POLICY_SHIFT_SCORE",
        source="TEST",
        payload={"score": 1.0, "scoring_version": version},
        retrieved_at=T0,
        known_at=T0,
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


def test_candles_are_folded_from_the_quotes_not_read_from_the_live_series():
    # market_bars holds the live series and never corrects a candle folded
    # across a gap a later tick backfill repaired, so a study reading it
    # would carry those candles into its returns.
    quotes = [
        Tick(
            symbol="USDJPY",
            bid=Decimal("150.00") + Decimal(i),
            ask=Decimal("150.01") + Decimal(i),
            time=T0 + timedelta(hours=6 * i),
            received_at=T0 + timedelta(hours=6 * i),
        )
        for i in range(9)
    ]

    bars = fold_daily(iter(quotes), "USDJPY", None)

    # Two whole days closed; the third is still open and is not a candle.
    assert [b.start for b in bars] == [T0, T0 + timedelta(days=1)]
    assert bars[0].timeframe == "1d"


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


def test_a_decision_older_than_the_series_has_no_entry():
    # The policy archive reaches back further than the prices do. Taking the
    # first close of the series would enter a years-old decision at the
    # series start and label that window its reaction.
    bars = [bar(i, "150.00") for i in range(3)]

    assert entry_bar(bars, T0 - timedelta(days=400), ANCHOR) is None


def test_only_the_scoring_version_this_build_computes_is_observed():
    # A re-tuned scorer re-ingests past meetings as new events, so one
    # meeting can sit in the store under several versions sharing a known_at.
    # Counting every version enters the same decision once per version.
    meeting = decision(SCORING_VERSION)
    superseded = decision("policy_shift_v0")

    assert current_version([meeting, superseded]) == [meeting]


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


def test_a_window_that_jumps_a_hole_in_the_series_is_not_measured():
    # A missing stretch leaves no candles, so a horizon counted in bars would
    # span it: five bars across the archive's 2026 hole is a ten-week move
    # reported as a week.
    # T0 is a Friday, so 0 -> 3 is a weekend and 3 -> 4 the next weekday.
    across_a_hole = [bar(0, "150.00"), bar(3, "150.50"), bar(40, "158.00")]

    assert window_outcome(across_a_hole, 0, 2) is None
    # A weekend is the series closing, not missing.
    over_a_weekend = [bar(0, "150.00"), bar(3, "150.50"), bar(4, "150.80")]
    assert window_outcome(over_a_weekend, 0, 2) is not None


def test_gaps_names_the_stretch_the_series_jumps():
    series = [bar(0, "150.00"), bar(3, "150.50"), bar(40, "158.00")]

    assert [(b.start.date(), a.start.date()) for b, a in gaps(series)] == [
        (bar(3, "0").start.date(), bar(40, "0").start.date())
    ]


def test_a_days_closure_is_reported_but_still_measured():
    # The horizons are trading days, so a day the market did not trade is not
    # one of them: the window either side of New Year measures what it
    # claims. It is still reported, because the series cannot tell a closure
    # from a day the archive lost.
    # T0 is a Friday, so index 0 -> 3 is a weekend and 3 -> 4 a weekday.
    unbroken = [bar(0, "150.00"), bar(3, "150.10"), bar(4, "150.20")]
    assert irregular_steps(unbroken) == []

    one_day_shut = [bar(3, "150.00"), bar(5, "150.10"), bar(6, "150.20")]
    assert len(irregular_steps(one_day_shut)) == 1
    assert gaps(one_day_shut) == []
    assert window_outcome(one_day_shut, 0, 2) is not None


def test_two_decisions_priced_by_one_close_keep_the_later_state():
    # Both banks can publish before the same close — 2024-07-31 was such a
    # day — and that close prices in both, so the earlier state is stale.
    earlier = observation(4, BOJ_LEG, -0.01)
    later = Observation(
        at=earlier.at + timedelta(hours=6),
        entry_index=4,
        group=BOTH_LEGS,
        divergence=2.0,
        intervention=False,
        returns={5: -0.01},
        adverse={5: 0.01},
        favorable={5: -0.02},
    )

    assert collapse_same_entry([earlier, later]) == [later]


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


def test_a_decision_the_series_outruns_at_one_horizon_survives_at_the_others():
    # The newest meetings have five days of prices behind them but not
    # twenty; dropping them everywhere would thin the recent sample out of
    # the short horizons for no reason.
    recent = Observation(
        at=T0,
        entry_index=0,
        group=BOTH_LEGS,
        divergence=1.0,
        intervention=False,
        returns={5: -0.01},
        adverse={5: 0.01},
        favorable={5: -0.02},
    )

    assert thin([recent], 5) == [recent]
    assert thin([recent], 20) == []


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


def test_the_baseline_covers_the_stretch_the_decisions_reach_over():
    # The price archive can outrun the policy one at either end, and a
    # baseline over the whole of it compares the signal against years the
    # signal was never measured in.
    bars = [bar(i, "150.00") for i in range(100)]
    observations = [observation(10, BOTH_LEGS, -0.01), observation(20, BOTH_LEGS, 0.01)]

    span = measured_span(observations, bars, 5)

    assert [b.start for b in span] == [b.start for b in bars[10:26]]


def test_the_baseline_is_empty_without_a_decision_to_span():
    assert measured_span([], [bar(i, "150.00") for i in range(10)], 5) == ()


def test_the_slope_is_undefined_when_every_decision_carries_the_same_divergence():
    observations = [
        observation(0, BOTH_LEGS, 0.02, divergence=1.0),
        observation(10, BOTH_LEGS, -0.02, divergence=1.0),
    ]

    # NaN rather than a fabricated zero slope.
    assert math.isnan(divergence_slope(observations, 5))
