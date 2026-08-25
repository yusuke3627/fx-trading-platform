from tests.support import at
from trading.domain.risk import EventRiskMode
from trading.risk.event_risk import EventRiskCalendar, EventRiskWindow
from trading.strategy.base import StrategyHorizon

# What the source file would claim to be complete over. Declared, never
# inferred from where the windows happen to sit.
COVERS = (at(days=0), at(days=60))


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


def calendar(*windows: EventRiskWindow, covers=COVERS) -> EventRiskCalendar:
    return EventRiskCalendar(list(windows), covers)


def test_normal_inside_the_covered_span_with_no_window_active():
    # The schedule is recorded here and recorded as quiet, which is a
    # different statement from having no record at all.
    assert (
        calendar(cluster_window()).mode_for(StrategyHorizon.SCALP, at(days=20))
        is EventRiskMode.NORMAL
    )


def test_nothing_is_claimed_outside_the_covered_span():
    # Past what the file says it covers, the answer is "not written down".
    # The caller falls back to its configured default rather than being told
    # all is well.
    known = calendar(cluster_window())

    assert known.mode_for(StrategyHorizon.SCALP, at(days=61)) is None
    assert known.mode_for(StrategyHorizon.SCALP, at(days=-1)) is None


def test_a_calendar_claiming_nothing_answers_nothing():
    # A source that declares no coverage cannot be read as quiet anywhere,
    # however many windows it happens to carry.
    unclaimed = EventRiskCalendar([cluster_window()])

    assert unclaimed.mode_for(StrategyHorizon.SCALP, at(days=11)) is None


def test_pre_and_post_hours_extend_the_window():
    known = calendar(cluster_window())

    assert known.mode_for(StrategyHorizon.SCALP, at(days=8, hours=1)) is EventRiskMode.HALT
    assert (
        known.mode_for(StrategyHorizon.INTRADAY, at(days=13, hours=20))
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

    known = calendar(cluster_window(), surprise)

    assert known.mode_for(StrategyHorizon.INTRADAY, at(days=11)) is EventRiskMode.HALT


def test_horizon_without_action_defaults_to_normal():
    window = EventRiskWindow(
        name="minor_release",
        first_event_at=at(days=1),
        last_event_at=at(days=1),
        pre_hours=1,
        post_hours=1,
        actions={StrategyHorizon.SCALP: EventRiskMode.HALT},
    )

    known = calendar(window)

    assert known.mode_for(StrategyHorizon.SWING, at(days=1)) is EventRiskMode.NORMAL
