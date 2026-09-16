from datetime import UTC, datetime, timedelta, timezone

import pytest

from trading.indicators.session import Session, session_end, session_start, sessions_at


def test_tokyo_session_is_active_only_in_its_local_window() -> None:
    assert Session.TOKYO in sessions_at(datetime(2026, 1, 15, 0, 30, tzinfo=UTC))
    assert Session.TOKYO not in sessions_at(datetime(2026, 1, 14, 23, 30, tzinfo=UTC))
    assert Session.TOKYO not in sessions_at(datetime(2026, 1, 15, 9, 0, tzinfo=UTC))
    assert session_start(
        Session.TOKYO, datetime(2026, 1, 15, 5, 0, tzinfo=UTC)
    ) == datetime(2026, 1, 15, 0, 0, tzinfo=UTC)


def test_london_session_follows_dst() -> None:
    assert Session.LONDON in sessions_at(datetime(2026, 7, 15, 7, 30, tzinfo=UTC))
    assert Session.LONDON not in sessions_at(datetime(2026, 1, 15, 7, 30, tzinfo=UTC))
    assert Session.LONDON in sessions_at(datetime(2026, 1, 15, 8, 30, tzinfo=UTC))


def test_london_session_follows_dst_transition_days() -> None:
    assert Session.LONDON in sessions_at(datetime(2026, 3, 29, 7, 30, tzinfo=UTC))
    assert Session.LONDON not in sessions_at(datetime(2026, 3, 28, 7, 30, tzinfo=UTC))
    assert Session.LONDON not in sessions_at(datetime(2026, 10, 25, 7, 30, tzinfo=UTC))
    assert Session.LONDON in sessions_at(datetime(2026, 10, 24, 7, 30, tzinfo=UTC))


def test_london_session_start_follows_dst() -> None:
    assert session_start(
        Session.LONDON, datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    ) == datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
    assert session_start(
        Session.LONDON, datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    ) == datetime(2026, 7, 15, 7, 0, tzinfo=UTC)


def test_new_york_session_follows_dst() -> None:
    assert Session.NEW_YORK in sessions_at(datetime(2026, 7, 15, 12, 30, tzinfo=UTC))
    assert Session.NEW_YORK not in sessions_at(datetime(2026, 1, 15, 12, 30, tzinfo=UTC))
    assert Session.NEW_YORK in sessions_at(datetime(2026, 1, 15, 13, 30, tzinfo=UTC))


def test_new_york_session_follows_dst_transition_days() -> None:
    assert Session.NEW_YORK in sessions_at(datetime(2026, 3, 8, 12, 30, tzinfo=UTC))
    assert Session.NEW_YORK not in sessions_at(datetime(2026, 3, 7, 12, 30, tzinfo=UTC))
    assert Session.NEW_YORK not in sessions_at(datetime(2026, 11, 1, 12, 30, tzinfo=UTC))
    assert Session.NEW_YORK in sessions_at(datetime(2026, 10, 31, 12, 30, tzinfo=UTC))


def test_new_york_session_start_follows_dst() -> None:
    assert session_start(
        Session.NEW_YORK, datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
    ) == datetime(2026, 1, 15, 13, 0, tzinfo=UTC)
    assert session_start(
        Session.NEW_YORK, datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
    ) == datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def test_session_results_are_independent_of_fixed_broker_offset() -> None:
    utc_timestamp = datetime(2026, 7, 15, 12, 30, tzinfo=UTC)
    server_timestamp = utc_timestamp.astimezone(timezone(timedelta(hours=3)))

    assert sessions_at(server_timestamp) == sessions_at(utc_timestamp)
    for session in Session:
        assert session_start(session, server_timestamp) == session_start(session, utc_timestamp)
        assert session_end(session, server_timestamp) == session_end(session, utc_timestamp)


def test_session_start_uses_previous_local_day_before_window() -> None:
    start = session_start(
        Session.TOKYO, datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
    )

    assert start == datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    assert start.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("session", "timestamp", "expected"),
    [
        (Session.LONDON, "2026-01-15T12:00", "2026-01-15T17:00"),
        (Session.LONDON, "2026-07-15T12:00", "2026-07-15T16:00"),
        (Session.NEW_YORK, "2026-01-15T18:00", "2026-01-15T22:00"),
        (Session.NEW_YORK, "2026-07-15T18:00", "2026-07-15T21:00"),
        (Session.TOKYO, "2026-01-15T05:00", "2026-01-15T09:00"),
        (Session.LONDON, "2026-03-27T12:00", "2026-03-27T17:00"),
        (Session.LONDON, "2026-03-30T12:00", "2026-03-30T16:00"),
        (Session.TOKYO, "2026-01-15T23:30", "2026-01-15T09:00"),
        (Session.LONDON, "2026-01-15T07:30", "2026-01-14T17:00"),
        (Session.NEW_YORK, "2026-01-15T12:30", "2026-01-14T22:00"),
    ],
)
def test_session_end_selects_the_same_nine_hour_window_as_start(session, timestamp, expected):
    timestamp = datetime.fromisoformat(timestamp).replace(tzinfo=UTC)
    end = session_end(session, timestamp)
    assert end == datetime.fromisoformat(expected).replace(tzinfo=UTC)
    assert end.utcoffset() == timedelta(0)
    assert end - session_start(session, timestamp) == timedelta(hours=9)


@pytest.mark.parametrize("session", list(Session))
def test_session_end_at_window_boundaries(session):
    timestamp = datetime(2026, 1, 15, 15, tzinfo=UTC)
    start = session_start(session, timestamp)
    end = session_end(session, timestamp)
    assert session_end(session, start) == end
    assert session_end(session, end) == end
    assert session in sessions_at(start)
    assert session not in sessions_at(end)
