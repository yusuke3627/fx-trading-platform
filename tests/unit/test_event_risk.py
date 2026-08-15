from tests.support import at
from trading.domain.risk import EventRiskMode
from trading.risk.event_risk import EventRiskCalendar, EventRiskWindow
from trading.strategy.base import StrategyHorizon


def cluster_window() -> EventRiskWindow:
    # FOMC + BOJ back-to-back: one independent risk state, not a sum.
    return EventRiskWindow(
        name="dual_central_bank_cluster",
        first_event_at=at(days=10),
        last_event_at=at(days=13),
        pre_hours=48,
        post_hours=24,
        actions={
            StrategyHorizon.SCALP: EventRiskMode.HALT,
            StrategyHorizon.INTRADAY: EventRiskMode.REDUCED,
            StrategyHorizon.SWING: EventRiskMode.REDUCED,
        },
    )


def test_normal_outside_window():
    calendar = EventRiskCalendar([cluster_window()])
    assert calendar.mode_for(StrategyHorizon.SCALP, at(days=7)) is EventRiskMode.NORMAL
    assert calendar.mode_for(StrategyHorizon.SCALP, at(days=15)) is EventRiskMode.NORMAL


def test_pre_and_post_hours_extend_the_window():
    calendar = EventRiskCalendar([cluster_window()])
    assert calendar.mode_for(StrategyHorizon.SCALP, at(days=8, hours=1)) is EventRiskMode.HALT
    assert (
        calendar.mode_for(StrategyHorizon.INTRADAY, at(days=13, hours=20))
        is EventRiskMode.REDUCED
    )


def test_most_severe_mode_wins_across_overlapping_windows():
    surprise = EventRiskWindow(
        name="surprise_speech",
        first_event_at=at(days=11),
        last_event_at=at(days=11),
        pre_hours=2,
        post_hours=2,
        actions={StrategyHorizon.INTRADAY: EventRiskMode.HALT},
    )
    calendar = EventRiskCalendar([cluster_window(), surprise])
    assert calendar.mode_for(StrategyHorizon.INTRADAY, at(days=11)) is EventRiskMode.HALT


def test_horizon_without_action_defaults_to_normal():
    window = EventRiskWindow(
        name="minor_release",
        first_event_at=at(days=1),
        last_event_at=at(days=1),
        pre_hours=1,
        post_hours=1,
        actions={StrategyHorizon.SCALP: EventRiskMode.HALT},
    )
    calendar = EventRiskCalendar([window])
    assert calendar.mode_for(StrategyHorizon.SWING, at(days=1)) is EventRiskMode.NORMAL
