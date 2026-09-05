"""実時間ショックトリガースタディの検出、層別、集計を検証する。"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.support import make_tick
from trading.backtest.intervention_event_study import Episode
from trading.backtest.policy_event_study import Stats, window_outcome
from trading.backtest.research import broker_label_to_known
from trading.backtest.shock_trigger_study import (
    GROSS,
    H2,
    H2_STRONG,
    H3,
    HORIZONS,
    LAYER_INTERVENTION,
    LAYER_OTHER,
    LAYER_POLICY,
    LOOKBACKS,
    NET,
    NOT_TRADABLE,
    PRIMARY,
    REJECTED,
    THRESHOLDS,
    CellResult,
    Provenance,
    QuoteBar,
    Spec,
    Trigger,
    classify_layer,
    detect,
    fold_quote_bars,
    intervention_windows,
    judge,
    log_returns,
    policy_windows,
    report,
    verdict,
    z_scores,
)
from trading.domain.event import EventEnvelope
from trading.domain.market import Bar

T0 = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
ANCHOR = timedelta(hours=7)


def q5(
    index: int,
    bid_close: str,
    ask_close: str | None = None,
    *,
    high: str | None = None,
    low: str | None = None,
    start: datetime | None = None,
) -> QuoteBar:
    bar_start = start or T0 + timedelta(minutes=5 * index)
    close = Decimal(bid_close)
    return QuoteBar(
        bar=Bar(
            symbol="USDJPY",
            timeframe="5m",
            start=bar_start,
            open=close,
            high=Decimal(high) if high is not None else close,
            low=Decimal(low) if low is not None else close,
            close=close,
            known_at=bar_start + timedelta(minutes=5),
        ),
        ask_close=Decimal(ask_close) if ask_close is not None else close + Decimal("0.010"),
    )


def series(closes: Sequence[str], spread: str = "0.010") -> list[QuoteBar]:
    return [
        q5(index, close, str(Decimal(close) + Decimal(spread)))
        for index, close in enumerate(closes)
    ]


def policy_event(known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type="BOJ_POLICY_SHIFT_SCORE",
        source="TEST",
        payload={"scoring_version": "policy_shift_v1"},
        retrieved_at=known_at,
        known_at=known_at,
    )


def test_fold_quote_bars_pairs_bid_close_with_last_ask() -> None:
    ticks = [
        make_tick("150.000", "150.010", T0),
        make_tick("150.100", "150.130", T0 + timedelta(minutes=2)),
        make_tick("150.050", "150.060", T0 + timedelta(minutes=4)),
        make_tick("150.200", "150.210", T0 + timedelta(minutes=5)),
    ]

    bars, provenance = fold_quote_bars(iter(ticks), "USDJPY", None)

    assert len(bars) == 1
    assert bars[0].bar.close == Decimal("150.050")
    assert bars[0].ask_close == Decimal("150.060")
    assert provenance.tick_count == 4
    assert provenance.first_tick == T0
    assert provenance.last_tick == T0 + timedelta(minutes=5)


def test_fold_quote_bars_ignores_straggler_for_ask() -> None:
    ticks = [
        make_tick("150.000", "150.010", T0),
        make_tick("150.100", "150.110", T0 + timedelta(minutes=5)),
        make_tick("149.000", "149.900", T0 + timedelta(minutes=2)),
        make_tick("150.200", "150.210", T0 + timedelta(minutes=10)),
    ]

    bars, _ = fold_quote_bars(iter(ticks), "USDJPY", None)

    assert [bar.ask_close for bar in bars] == [
        Decimal("150.010"),
        Decimal("150.110"),
    ]


def test_log_returns_skip_non_contiguous_pairs() -> None:
    bars = [q5(0, "100"), q5(1, "101"), q5(2, "99")]
    later = T0 + timedelta(days=2)
    bars.extend([q5(3, "98", start=later), q5(4, "97", start=later + timedelta(minutes=5))])

    returns = log_returns(bars)

    assert returns[0] is None
    assert returns[1] == pytest.approx(math.log(101 / 100))
    assert returns[2] == pytest.approx(math.log(99 / 101))
    assert returns[3] is None
    assert returns[4] == pytest.approx(math.log(97 / 98))


def test_z_scores_need_lookback_valid_returns_and_exclude_current() -> None:
    returns = [None, 0.01, -0.01, 0.02, -0.02, None, 0.03]
    prior = returns[1:5]
    mean = sum(prior) / 4
    deviation = math.sqrt(sum((value - mean) ** 2 for value in prior) / 4)

    scores = z_scores(returns, lookback=4)

    assert scores[:6] == [None] * 6
    assert scores[6] == pytest.approx((0.03 - mean) / deviation)


@pytest.mark.parametrize(
    ("score", "current_return", "expected"),
    [
        (-4.0, -0.001, 0),
        (-4.01, -0.001, 1),
        (-6.0, 0.0001, 0),
    ],
)
def test_trigger_fires_only_below_threshold_with_negative_return(
    score: float, current_return: float, expected: int
) -> None:
    bars = series(["150"] * 60)
    returns: list[float | None] = [0.0] * len(bars)
    scores: list[float | None] = [0.0] * len(bars)
    returns[5] = current_return
    scores[5] = score

    result = detect(
        bars,
        [quote.bar for quote in bars],
        returns,
        scores,
        Spec(48, 4.0),
        [],
        [],
    )

    assert len(result.triggers) == expected


def test_second_trigger_within_forward_window_is_suppressed() -> None:
    bars = series(["150"] * 120)
    returns: list[float | None] = [0.0] * len(bars)
    scores: list[float | None] = [0.0] * len(bars)
    for entry in (5, 53, 54):
        returns[entry] = -0.001
        scores[entry] = -5.0

    result = detect(
        bars,
        [quote.bar for quote in bars],
        returns,
        scores,
        Spec(48, 4.0),
        [],
        [],
    )

    assert [trigger.entry for trigger in result.triggers] == [5, 54]
    assert result.suppressed == 1


def test_trigger_with_gap_inside_forward_window_is_invalid() -> None:
    bars = series(["150"] * 80)
    bars[25:] = [
        q5(index, "150", start=quote.bar.start + timedelta(minutes=5))
        for index, quote in enumerate(bars[25:], start=25)
    ]
    returns = log_returns(bars)
    scores: list[float | None] = [0.0] * len(bars)
    scores[5] = -5.0
    returns[5] = -0.001

    gap_result = detect(
        bars,
        [quote.bar for quote in bars],
        returns,
        scores,
        Spec(48, 4.0),
        [],
        [],
    )

    trailing_bars = series(["150"] * 60)
    trailing_returns: list[float | None] = [0.0] * len(trailing_bars)
    trailing_scores: list[float | None] = [0.0] * len(trailing_bars)
    trailing_returns[20] = -0.001
    trailing_scores[20] = -5.0
    trailing_result = detect(
        trailing_bars,
        [quote.bar for quote in trailing_bars],
        trailing_returns,
        trailing_scores,
        Spec(48, 4.0),
        [],
        [],
    )

    assert gap_result.triggers == []
    assert gap_result.invalid == 1
    assert trailing_result.triggers == []
    assert trailing_result.invalid == 1


def test_net_is_settled_at_ask_and_spread_is_recorded() -> None:
    bars = [
        q5(
            index,
            str(Decimal(150) - Decimal(index) / 100),
            str(Decimal("150.020") - Decimal(index) / 100),
            high=str(Decimal("150.100") - Decimal(index) / 100),
            low=str(Decimal("149.900") - Decimal(index) / 100),
        )
        for index in range(60)
    ]
    returns: list[float | None] = [0.0] * len(bars)
    scores: list[float | None] = [0.0] * len(bars)
    returns[5] = -0.001
    scores[5] = -5.0

    result = detect(
        bars,
        [quote.bar for quote in bars],
        returns,
        scores,
        Spec(48, 4.0),
        [],
        [],
    )

    trigger = result.triggers[0]
    assert trigger.spread == Decimal("0.020")
    for horizon in HORIZONS:
        expected = window_outcome([quote.bar for quote in bars], 5, horizon.bars)
        assert trigger.returns[NET][horizon.label] - trigger.returns[GROSS][
            horizon.label
        ] == pytest.approx(
            math.log(
                float(bars[5 + horizon.bars].ask_close)
                / float(bars[5 + horizon.bars].bar.close)
            )
        )
        assert trigger.adverse[horizon.label] == expected[1]
        assert trigger.favorable[horizon.label] == expected[2]


def test_layers_assign_intervention_policy_other_and_overlap_goes_to_a() -> None:
    first_date = T0.date()
    second_date = first_date + timedelta(days=1)
    episodes = [
        Episode(first_date, T0, first_date),
        Episode(second_date, T0 + timedelta(days=1), first_date),
    ]
    interventions = intervention_windows(episodes)
    policy_label = T0 + timedelta(hours=1)
    known_at = broker_label_to_known(policy_label, ANCHOR)
    policies = policy_windows([policy_event(known_at)], ANCHOR)

    first = classify_layer(q5(1, "150").bar, interventions, [])
    overlap_only = classify_layer(
        q5(0, "150", start=T0 + timedelta(hours=37)).bar, interventions, []
    )
    at_publication = classify_layer(
        q5(0, "150", start=policy_label).bar, [], policies
    )
    other = classify_layer(
        q5(0, "150", start=T0 + timedelta(days=4)).bar, interventions, policies
    )
    overlap = classify_layer(
        q5(0, "150", start=policy_label).bar, interventions, policies
    )

    assert first == (LAYER_INTERVENTION, True, False)
    assert overlap_only == (LAYER_INTERVENTION, False, False)
    assert at_publication == (LAYER_POLICY, False, False)
    assert other == (LAYER_OTHER, False, False)
    assert overlap == (LAYER_INTERVENTION, True, True)


def stats(low: float, high: float) -> Stats:
    return Stats(2, (low + high) / 2, 0.0, 0.5, 0.0, 0.0, low, high)


@pytest.mark.parametrize(
    ("a_net", "c_net", "a_gross", "expected"),
    [
        (stats(-0.3, -0.1), stats(-0.1, 0.1), stats(-0.3, -0.1), H2),
        (stats(-0.3, -0.1), stats(-0.4, -0.2), stats(-0.3, -0.1), H3),
        (stats(-0.3, -0.1), stats(0.1, 0.3), stats(-0.3, -0.1), H2_STRONG),
        (stats(-0.1, 0.1), stats(-0.1, 0.1), stats(-0.3, -0.1), NOT_TRADABLE),
        (stats(-0.1, 0.1), stats(-0.1, 0.1), stats(-0.1, 0.1), None),
    ],
)
def test_judge_covers_every_branch(
    a_net: Stats, c_net: Stats, a_gross: Stats, expected: str | None
) -> None:
    assert judge(a_net, c_net, a_gross) == expected


def trigger(entry: int, layer: str, one_hour: float, four_hours: float) -> Trigger:
    values = {"15m": 0.0, "1h": one_hour, "4h": four_hours}
    excursions = {horizon.label: 0.0 for horizon in HORIZONS}
    return Trigger(
        entry=entry,
        z=-5.0,
        ret=-0.01,
        layer=layer,
        first=layer == LAYER_INTERVENTION,
        overlap=False,
        spread=Decimal("0.010"),
        returns={GROSS: dict(values), NET: dict(values)},
        adverse=excursions,
        favorable=excursions,
    )


def test_verdict_uses_four_hours_after_one_hour_has_no_result() -> None:
    cell = CellResult(
        PRIMARY,
        [
            trigger(0, LAYER_INTERVENTION, 0.0, -0.2),
            trigger(60, LAYER_INTERVENTION, 0.0, -0.2),
            trigger(120, LAYER_OTHER, 0.0, -0.2),
            trigger(180, LAYER_OTHER, 0.0, 0.2),
        ],
        0,
        0,
    )
    rejected = CellResult(
        PRIMARY,
        [
            trigger(0, LAYER_INTERVENTION, 0.0, 0.0),
            trigger(60, LAYER_OTHER, 0.0, 0.0),
        ],
        0,
        0,
    )

    assert verdict(cell, 42) == (H2, "4h")
    assert verdict(rejected, 42) == (REJECTED, None)


def test_report_lists_every_cell_and_the_verdict() -> None:
    closes = [
        str(Decimal(150) + (Decimal("0.001") if index % 2 else Decimal(0)))
        for index in range(300)
    ]
    closes.extend(["145"] + ["145"] * 99)
    bars = series(closes)
    bid_bars = [quote.bar for quote in bars]
    returns = log_returns(bars)
    episodes = [Episode(T0.date(), T0, T0.date())]
    interventions = intervention_windows(episodes)
    cells = []
    for lookback in LOOKBACKS:
        scores = z_scores(returns, lookback)
        for threshold in THRESHOLDS:
            cells.append(
                detect(
                    bars,
                    bid_bars,
                    returns,
                    scores,
                    Spec(lookback, threshold),
                    interventions,
                    [],
                )
            )

    output = report(
        cells,
        bars,
        bid_bars,
        episodes,
        [],
        Provenance(400, T0, bars[-1].bar.close_time, "dataset-test"),
        {"git_commit": "test", "git_dirty": False},
        ANCHOR,
        42,
    )

    grid_rows = [
        line
        for line in output.splitlines()
        if re.match(r"^\s+(48|96|288)\s+[345]\s+", line)
    ]
    details = output.split("primary N=96 K=4 layer A triggers", maxsplit=1)[1]
    assert len(grid_rows) == 9
    assert f"{bars[300].bar.start.isoformat()} |" in details
    assert "verdict (primary N=96 K=4, net):" in output
