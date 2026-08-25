"""Event-risk windows from the meeting calendar: consecutive decisions merge."""
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from trading.config import EventRiskWindowSettings, load_config
from trading.data.policy.meetings import PolicyMeeting, ScheduledMeeting
from trading.data.policy.risk_windows import (
    CENTRAL_BANK_CLUSTER,
    central_bank_calendar,
    central_bank_windows,
)
from trading.domain.risk import EventRiskMode
from trading.strategy.base import StrategyHorizon

T0 = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)

SETTINGS = EventRiskWindowSettings(
    pre_hours=48,
    post_hours=24,
    scalp=EventRiskMode.HALT,
    intraday=EventRiskMode.REDUCED,
    swing=EventRiskMode.REDUCED,
)


def meeting(
    bank: str, published_at: datetime, decision_date: date | None = None
) -> PolicyMeeting:
    return PolicyMeeting(
        bank=bank,
        decision_date=decision_date if decision_date is not None else published_at.date(),
        statement_published_at=published_at,
        verified=False,
        source_uri="https://example.invalid/statement",
    )


def scheduled(
    bank: str, earliest: datetime, latest: datetime | None = None
) -> ScheduledMeeting:
    return ScheduledMeeting(
        bank=bank,
        decision_date=earliest.date(),
        earliest_published_at=earliest,
        latest_published_at=latest if latest is not None else earliest,
        source_uri="https://example.invalid/calendar",
    )


def test_no_meetings_produce_no_windows():
    assert central_bank_windows([], [], SETTINGS) == []


def test_an_announced_meeting_opens_a_window_before_its_results_exist():
    # The market braces for the date, not the outcome: schedule-only entries
    # must open windows exactly like transcribed ones.
    (window,) = central_bank_windows([], [scheduled("FED", T0)], SETTINGS)

    assert window.first_event_at == T0


def test_an_uncertain_publication_time_widens_the_window_not_shifts_it():
    # BOJ publishes with no fixed clock time. Hanging the window off the late
    # bound alone would open the pre-window hours late; off the early bound
    # alone it would close hours early. The interval covers both.
    early = T0
    late = T0 + timedelta(hours=6)

    (window,) = central_bank_windows([], [scheduled("BOJ", early, late)], SETTINGS)

    assert window.first_event_at == early
    assert window.last_event_at == late


def test_a_publication_interval_wider_than_the_padding_stays_one_window():
    # A meeting is one span however its uncertainty compares to pre + post.
    # Clustering the bounds as separate instants would split the window and
    # grade the middle — where the statement can land — as NORMAL.
    early = T0
    late = T0 + timedelta(hours=200)

    (window,) = central_bank_windows([], [scheduled("BOJ", early, late)], SETTINGS)

    assert window.first_event_at == early
    assert window.last_event_at == late


def test_a_transcribed_meeting_keeps_its_announced_window():
    # Once the results are in, the actual publication minute is known — but
    # pre-positioning was decided against the announced interval. Rebuilding
    # the window from the actual minute would let a replay open the HALT hours
    # later than live did, on knowledge nobody had at the time.
    announced = scheduled("BOJ", T0, T0 + timedelta(hours=6))
    actual = meeting("BOJ", T0 + timedelta(hours=2))

    (window,) = central_bank_windows([actual], [announced], SETTINGS)

    assert window.first_event_at == T0
    assert window.last_event_at == T0 + timedelta(hours=6)


def test_a_late_actual_publication_does_not_stretch_the_window():
    # These windows are not gated by query time, so stretching the end to a
    # late actual publication would HALT a replay of the hours before the
    # statement landed — hours live graded NORMAL — on knowledge from later.
    # Bounds a statement escaped were curated wrong; that is a data fix.
    announced = scheduled("BOJ", T0, T0 + timedelta(hours=6))
    actual = meeting("BOJ", T0 + timedelta(hours=9), decision_date=T0.date())

    (window,) = central_bank_windows([actual], [announced], SETTINGS)

    assert window.last_event_at == T0 + timedelta(hours=6)


def test_the_shipped_schedule_reaches_the_calendar():
    calendar = central_bank_calendar(load_config("shadow"))

    assert calendar is not None
    # The September FOMC instant is recorded in the shipped schedule: section,
    # inside the declared coverage.
    sept_fomc = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
    assert calendar.mode_for(StrategyHorizon.SCALP, sept_fomc) is EventRiskMode.HALT


def test_no_configured_window_yields_no_calendar():
    # Not the same as an empty calendar. An empty one says the schedule is
    # known and nothing is near; None says the schedule is not known, and the
    # caller falls back to its configured default.
    assert central_bank_calendar(SimpleNamespace(event_risk={})) is None


def test_a_configured_window_grades_against_the_shipped_meetings():
    config = load_config("shadow")

    calendar = central_bank_calendar(config)

    assert calendar is not None
    # T0 is the Fed decision instant recorded in the shipped file, inside the
    # coverage it declares.
    assert calendar.mode_for(StrategyHorizon.SCALP, T0) is EventRiskMode.HALT


def test_the_shipped_calendar_claims_nothing_beyond_its_coverage():
    # The file records July 2026 and says so. A year later it has nothing to
    # offer, and risk falls back to its configured default rather than reading
    # the silence as quiet.
    calendar = central_bank_calendar(load_config("shadow"))

    assert calendar is not None
    assert calendar.mode_for(StrategyHorizon.SCALP, T0 + timedelta(days=365)) is None


def test_a_single_meeting_becomes_one_window():
    (window,) = central_bank_windows([meeting("FED", T0)], [], SETTINGS)

    assert window.name == CENTRAL_BANK_CLUSTER
    assert window.first_event_at == window.last_event_at == T0
    assert window.pre_hours == 48
    assert window.post_hours == 24


def test_decisions_close_together_become_one_window():
    # A Fed decision and a BOJ decision two days apart are one risk state, not
    # two: each sits inside the other's window, and grading them separately
    # would let the gap between them read as calm.
    boj = T0 + timedelta(days=2)

    (window,) = central_bank_windows([meeting("FED", T0), meeting("BOJ", boj)], [], SETTINGS)

    assert window.first_event_at == T0
    assert window.last_event_at == boj


def test_decisions_far_apart_stay_separate():
    later = T0 + timedelta(days=40)

    windows = central_bank_windows(
        [meeting("FED", T0), meeting("BOJ", later)], [], SETTINGS
    )

    assert [w.first_event_at for w in windows] == [T0, later]


def test_meetings_out_of_order_still_cluster_by_time():
    # The file is edited by hand and need not be sorted.
    boj = T0 + timedelta(days=2)

    (window,) = central_bank_windows([meeting("BOJ", boj), meeting("FED", T0)], [], SETTINGS)

    assert window.first_event_at == T0
    assert window.last_event_at == boj


def test_each_horizon_carries_its_configured_action():
    (window,) = central_bank_windows([meeting("FED", T0)], [], SETTINGS)

    assert window.actions == {
        StrategyHorizon.SCALP: EventRiskMode.HALT,
        StrategyHorizon.INTRADAY: EventRiskMode.REDUCED,
        StrategyHorizon.SWING: EventRiskMode.REDUCED,
    }


def test_the_window_hangs_off_publication_not_the_decision_date():
    # A decision is risk from the moment it can move the market. The BOJ files
    # its 15:00 JST statement under a date that began nine hours earlier.
    published = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)

    (window,) = central_bank_windows([meeting("BOJ", published)], [], SETTINGS)

    assert window.first_event_at == published
    assert window.active_at(published - timedelta(hours=47))
    assert not window.active_at(published - timedelta(hours=49))
