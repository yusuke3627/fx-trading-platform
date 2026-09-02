"""介入イベントスタディのアンカー、窓、集計の組み立て。

価格・時刻・イベントはすべて合成データである。
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading.backtest.intervention_event_study import (
    SHOCK,
    Anchor,
    Episode,
    Horizon,
    Outcome,
    baseline_span,
    build_outcome,
    build_outcomes,
    business_days_between,
    cluster_anchors,
    cluster_only,
    daily_entry,
    fold_bars,
    load_episodes_from_events,
    news_anchor,
    path,
    report,
    shock_anchors,
    stats,
)
from trading.data.market.dukascopy import known_to_broker_label
from trading.domain.event import EventEnvelope
from trading.domain.market import Bar, Tick

T0 = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
T0_DATE = T0.date()
ANCHOR = timedelta(hours=7)


def m5(
    index: int,
    close: str,
    *,
    open: str | None = None,
    high: str | None = None,
    low: str | None = None,
) -> Bar:
    start = T0 + timedelta(minutes=5 * index)
    return Bar(
        symbol="USDJPY",
        timeframe="5m",
        start=start,
        open=Decimal(open or close),
        high=Decimal(high or close),
        low=Decimal(low or close),
        close=Decimal(close),
        known_at=start + timedelta(minutes=5),
    )


def d1(
    index: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    day_offset: int = 0,
) -> Bar:
    start = T0 + timedelta(days=index + day_offset)
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


def episode(
    action_date: date = T0_DATE,
    *,
    known_at: datetime | None = None,
    cluster: date | None = None,
) -> Episode:
    return Episode(
        action_date=action_date,
        known_at=known_at or T0,
        cluster=cluster or action_date,
    )


def intervention_event(action_date: date, direction: str, known_at: datetime) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type="INTERVENTION_REPORTED",
        source="TEST",
        payload={"action_date": action_date.isoformat(), "direction": direction},
        retrieved_at=known_at,
        known_at=known_at,
    )


def test_events_are_sorted_filtered_and_assigned_to_clusters() -> None:
    first_date = date(2026, 4, 30)
    second_date = date(2026, 5, 4)
    events = [
        intervention_event(second_date, "JPY_BUY", T0 + timedelta(days=1)),
        intervention_event(first_date, "JPY_BUY", T0),
        intervention_event(date(2026, 5, 6), "JPY_SELL", T0 + timedelta(days=2)),
    ]

    episodes = load_episodes_from_events(events)

    assert [item.action_date for item in episodes] == [first_date, second_date]
    assert [item.cluster for item in episodes] == [first_date, first_date]


def test_fold_bars_builds_five_minute_and_daily_series_in_one_pass() -> None:
    ticks = [
        Tick(
            symbol="USDJPY",
            bid=Decimal(f"{150 + index / 100:.2f}"),
            ask=Decimal(f"{150.01 + index / 100:.2f}"),
            time=T0 + offset,
            received_at=T0 + offset,
        )
        for index, offset in enumerate(
            (
                timedelta(),
                timedelta(minutes=4),
                timedelta(minutes=5),
                timedelta(days=1),
                timedelta(days=1, minutes=5),
                timedelta(days=2),
            )
        )
    ]

    series = fold_bars(iter(ticks), "USDJPY", ("5m", "1d"), None)

    assert [bar.start for bar in series["5m"]] == [
        T0,
        T0 + timedelta(minutes=5),
        T0 + timedelta(days=1),
        T0 + timedelta(days=1, minutes=5),
    ]
    assert [bar.start for bar in series["1d"]] == [T0, T0 + timedelta(days=1)]


def test_shock_anchor_uses_close_open_and_ignores_search_boundaries() -> None:
    bars = [
        m5(-1, "130", open="150", low="120"),
        m5(0, "149", open="150", low="149"),
        m5(1, "150", open="150", low="100"),
        m5(2, "147", open="150", low="146"),
        m5(432, "120", open="150", low="120"),
    ]

    anchor = shock_anchors(bars, [episode()])[T0.date()]

    assert anchor is not None
    assert bars[anchor.entry].start == T0 + timedelta(minutes=10)
    assert anchor.window_bars == 3
    assert anchor.drop == pytest.approx(math.log(147 / 150))


def test_shock_anchor_stops_at_the_next_episode_and_breaks_ties_early() -> None:
    next_date = T0.date() + timedelta(days=1)
    bars = [
        m5(0, "149", open="150"),
        m5(1, "148", open="150"),
        m5(2, "148", open="150"),
        m5(288, "130", open="150"),
        m5(720, "150", open="150"),
    ]

    anchors = shock_anchors(bars, [episode(), episode(next_date)])

    first = anchors[T0.date()]
    assert first is not None
    assert first.entry == 1
    assert first.window_bars == 3
    second = anchors[next_date]
    assert second is not None
    assert second.entry == 3


def test_shock_anchor_without_quotes_is_none() -> None:
    missing_date = T0.date() + timedelta(days=10)

    assert shock_anchors([m5(0, "150")], [episode(missing_date)])[missing_date] is None


def test_shock_anchor_requires_the_search_window_to_be_closed() -> None:
    partial = [m5(0, "149", open="150"), m5(431, "148", open="150")]

    assert shock_anchors(partial, [episode()])[T0.date()] is None
    assert shock_anchors([*partial, m5(432, "150")], [episode()])[T0.date()] is not None


def test_shock_anchor_rejects_a_search_window_with_an_archive_gap() -> None:
    bars = [m5(0, "149", open="150"), m5(1, "148", open="150"), m5(1441, "150")]

    assert shock_anchors(bars, [episode()])[T0.date()] is None


def test_shock_anchor_rejects_an_archive_gap_crossing_the_window_start() -> None:
    bars = [m5(-1440, "150"), m5(12, "148", open="150"), m5(432, "150")]

    assert shock_anchors(bars, [episode()])[T0.date()] is None


@pytest.mark.parametrize(
    ("known_at", "expected_label"),
    [
        (
            datetime(2026, 5, 4, 14, 59, tzinfo=UTC),
            datetime(2026, 5, 4, 17, 59, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 5, 14, 59, tzinfo=UTC),
            datetime(2026, 1, 5, 16, 59, tzinfo=UTC),
        ),
    ],
)
def test_news_anchor_converts_summer_and_winter_to_the_broker_label(
    known_at: datetime, expected_label: datetime
) -> None:
    bars = [
        Bar(
            symbol="USDJPY",
            timeframe="5m",
            start=expected_label.replace(minute=55),
            open=Decimal(150),
            high=Decimal(150),
            low=Decimal(150),
            close=Decimal(150),
            known_at=known_at,
        )
    ]
    event = episode(known_at.date(), known_at=known_at)

    anchor = news_anchor(bars, event, ANCHOR)

    assert known_to_broker_label(known_at, ANCHOR) == expected_label
    assert anchor is not None
    assert anchor.entry == 0


def test_news_anchor_requires_a_close_strictly_after_known_at() -> None:
    label = T0 + timedelta(minutes=5)
    known_at = datetime(2026, 5, 3, 21, 5, tzinfo=UTC)
    assert known_to_broker_label(known_at, ANCHOR) == label

    anchor = news_anchor([m5(0, "150"), m5(1, "151")], episode(known_at=known_at), ANCHOR)

    assert anchor is not None
    assert anchor.entry == 1


def test_news_anchor_rejects_a_bar_beyond_the_news_lag_limit() -> None:
    known_at = datetime(2026, 5, 3, 21, 5, tzinfo=UTC)
    event = episode(known_at=known_at)

    assert news_anchor([m5(863, "150")], event, ANCHOR) is not None
    assert news_anchor([m5(864, "150")], event, ANCHOR) is None


def test_daily_entry_includes_a_five_minute_bar_ending_at_the_daily_close() -> None:
    intraday = [m5(0, "150"), m5(287, "151"), m5(288, "152")]
    daily = [d1(0, "151"), d1(1, "152")]

    assert daily_entry(intraday, 0, daily) == 0
    assert daily_entry(intraday, 1, daily) == 0
    assert daily_entry(intraday, 2, daily) == 1
    assert daily_entry(intraday, 2, []) is None


def test_business_days_and_clusters_ignore_weekends_and_chain() -> None:
    assert business_days_between(date(2026, 5, 1), date(2026, 5, 4)) == 1
    assert business_days_between(date(2026, 4, 30), date(2026, 5, 7)) == 5
    assert business_days_between(date(2026, 4, 30), date(2026, 5, 8)) == 6

    dates_2022 = [date(2022, 9, 22), date(2022, 10, 21), date(2022, 10, 24)]
    assert cluster_anchors(dates_2022) == {
        date(2022, 9, 22): date(2022, 9, 22),
        date(2022, 10, 21): date(2022, 10, 21),
        date(2022, 10, 24): date(2022, 10, 21),
    }

    dates_2026 = [date(2026, 4, 30), date(2026, 5, 4), date(2026, 5, 6)]
    assert set(cluster_anchors(dates_2026).values()) == {date(2026, 4, 30)}


def test_build_outcome_uses_five_minute_and_daily_steps() -> None:
    intraday = [
        m5(index, str(price), high=str(price + 1), low=str(price - 1))
        for index, price in enumerate(range(150, 200))
    ]
    daily = [d1(index, str(150 + index)) for index in range(12)]
    anchor = Anchor(SHOCK, episode(), 0, 0.0, 50)

    outcome = build_outcome(anchor, {"5m": intraday, "1d": daily})

    assert outcome.returns["15m"] == pytest.approx(math.log(153 / 150))
    assert outcome.adverse["15m"] == pytest.approx(math.log(154 / 150))
    assert outcome.favorable["15m"] == pytest.approx(math.log(150 / 150))
    assert outcome.returns["1d"] == pytest.approx(math.log(151 / 150))
    assert "4h" in outcome.returns
    assert "10d" in outcome.returns


def test_build_outcome_omits_horizons_past_the_series_end() -> None:
    anchor = Anchor(SHOCK, episode(), 0, 0.0, 2)

    outcome = build_outcome(anchor, {"5m": [m5(0, "150"), m5(1, "151")], "1d": []})

    assert outcome.returns == {}
    assert outcome.daily_entry is None


def test_build_outcome_rejects_a_hole_but_measures_a_weekend() -> None:
    intraday = [m5(index, "150") for index in range(4)]
    anchor = Anchor(SHOCK, episode(), 0, 0.0, 4)
    across_hole = [d1(0, "150"), d1(1, "149"), d1(2, "148", day_offset=5)]
    over_weekend = [d1(0, "150"), d1(3, "149"), d1(4, "148")]

    hole = build_outcome(anchor, {"5m": intraday, "1d": across_hole})
    weekend = build_outcome(anchor, {"5m": intraday, "1d": over_weekend})

    assert "2d" not in hole.returns
    assert "2d" in weekend.returns


def test_path_is_cumulative_from_the_anchor_and_omits_unreachable_offsets() -> None:
    bars = [d1(0, "151"), d1(1, "149"), d1(2, "147")]

    values = path(bars, 0, (0, 1, 2, 3), 150.0)

    assert values == {
        0: pytest.approx(math.log(151 / 150)),
        1: pytest.approx(math.log(149 / 150)),
        2: pytest.approx(math.log(147 / 150)),
    }


def measured_outcome(action_date: date, cluster: date, entry: int, ret: float) -> Outcome:
    anchor = Anchor(
        kind=SHOCK,
        episode=episode(action_date, cluster=cluster),
        entry=entry,
        drop=-0.01,
        window_bars=10,
    )
    return Outcome(
        anchor=anchor,
        daily_entry=entry,
        returns={"15m": ret},
        adverse={"15m": abs(ret)},
        favorable={"15m": -abs(ret)},
        profile_intraday={3: ret},
        profile_daily={0: ret},
    )


def test_stats_and_baseline_span_use_cluster_anchors() -> None:
    first_date = T0.date()
    overlap_date = first_date + timedelta(days=1)
    last_date = first_date + timedelta(days=20)
    outcomes = [
        measured_outcome(first_date, first_date, 2, -0.02),
        measured_outcome(overlap_date, first_date, 4, 0.01),
        measured_outcome(last_date, last_date, 8, -0.01),
    ]

    summary = stats(outcomes, "15m", 1)
    non_overlapping = stats(cluster_only(outcomes), "15m", 1)
    span = baseline_span(outcomes, [m5(i, "150") for i in range(20)], Horizon("15m", "5m", 3))

    assert summary.count == 3
    assert summary.mean == pytest.approx(-0.02 / 3)
    assert summary.hit_rate == pytest.approx(2 / 3)
    # overlap 日を除くと 2 件、どちらも下落なので hit は 1.0 になる。
    assert [o.anchor.episode.action_date for o in cluster_only(outcomes)] == [first_date, last_date]
    assert non_overlapping.count == 2
    assert non_overlapping.hit_rate == pytest.approx(1.0)
    assert span[0].start == m5(2, "0").start
    assert span[-1].start == m5(11, "0").start


def test_baseline_span_ignores_cluster_anchors_without_the_horizon() -> None:
    first_date = T0.date()
    later_date = first_date + timedelta(days=20)
    measured = measured_outcome(first_date, first_date, 2, -0.01)
    unmeasured = replace(
        measured_outcome(later_date, later_date, 15, -0.01),
        returns={},
        adverse={},
        favorable={},
    )

    span = baseline_span(
        [measured, unmeasured],
        [m5(i, "150") for i in range(20)],
        Horizon("15m", "5m", 3),
    )

    assert span[0].start == m5(2, "0").start
    assert span[-1].start == m5(5, "0").start


def test_report_contains_missing_overlap_individual_summary_and_profile_sections() -> None:
    first = episode(T0.date(), known_at=datetime(2026, 5, 3, 21, 1, tzinfo=UTC))
    overlap_date = T0.date() + timedelta(days=1)
    overlap = episode(
        overlap_date,
        known_at=datetime(2026, 5, 4, 21, 1, tzinfo=UTC),
        cluster=T0.date(),
    )
    missing_date = T0.date() + timedelta(days=20)
    missing = episode(
        missing_date,
        known_at=datetime(2026, 5, 24, 21, 1, tzinfo=UTC),
    )
    intraday = [m5(index, f"{150 - index / 100:.2f}") for index in range(721)]
    daily = [d1(index, f"{150 - index / 10:.2f}") for index in range(15)]
    series = {"5m": intraday, "1d": daily}

    outcomes = build_outcomes([first, overlap, missing], series, ANCHOR)
    text = report(outcomes, series, [first, overlap, missing], ANCHOR)

    assert "shock anchors" in text
    assert "news anchors" in text
    assert "no shock anchor" in text
    assert f"overlap {T0.date()}" in text
    assert "shock episode details" in text
    assert "news summary" in text
    assert "decay profile (cumulative from the anchor close)" in text
    assert "entry-day close" in text
    # 5 分足オフセットは 15 分刻みなので、1 時間の倍数でない行にも固有のラベルが要る。
    assert "+1h15m" in text
    assert "+4h " in text
    assert "+10d" in text
