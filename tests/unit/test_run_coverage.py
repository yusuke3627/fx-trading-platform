from datetime import UTC, datetime

import pytest

from trading.backtest.run_coverage import run_coverage


def test_empty_generator_has_no_trade_times_and_all_months_are_empty():
    coverage = run_coverage(
        (at for at in ()),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert coverage.first_trade_at is None
    assert coverage.last_trade_at is None
    assert coverage.trailing_blackout_days is None
    assert coverage.months_with_trades == 0
    assert coverage.months_in_period == 6
    assert coverage.empty_months == (
        "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
    )


def test_trades_in_every_month_leave_no_empty_months():
    coverage = run_coverage(
        (datetime(2026, month, 15, 12, tzinfo=UTC) for month in range(1, 7)),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert coverage.first_trade_at == datetime(2026, 1, 15, 12, tzinfo=UTC)
    assert coverage.last_trade_at == datetime(2026, 6, 15, 12, tzinfo=UTC)
    assert coverage.months_with_trades == coverage.months_in_period == 6
    assert coverage.empty_months == ()
    assert coverage.trailing_blackout_days == 15.5
    assert isinstance(coverage.trailing_blackout_days, float)


def test_unsorted_duplicate_generator_preserves_entry_range_and_trailing_gap():
    first = datetime(2026, 1, 15, tzinfo=UTC)
    last = datetime(2026, 2, 15, tzinfo=UTC)
    coverage = run_coverage(
        (at for at in (last, first, last, first)),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert coverage.first_trade_at == first
    assert coverage.last_trade_at == last
    assert coverage.months_with_trades == 2
    assert coverage.months_in_period == 6
    assert coverage.empty_months == ("2026-03", "2026-04", "2026-05", "2026-06")
    assert coverage.trailing_blackout_days == 136.0


@pytest.mark.parametrize(
    ("period_from", "period_to", "months", "days"),
    [
        (
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC),
            ("2026-01",), 31.0,
        ),
        (
            datetime(2024, 12, 31, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC),
            ("2024-12", "2025-01"), 32.0,
        ),
        (
            datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC),
            ("2024-02",), 29.0,
        ),
        (
            datetime(2026, 1, 15, 12, tzinfo=UTC), datetime(2026, 3, 15, 18, tzinfo=UTC),
            ("2026-01", "2026-02", "2026-03"), 59.25,
        ),
    ],
)
def test_month_boundaries_year_rollover_and_leap_day(period_from, period_to, months, days):
    coverage = run_coverage((at for at in (period_from,)), period_from, period_to)

    assert coverage.first_trade_at == coverage.last_trade_at == period_from
    assert coverage.months_in_period == len(months)
    assert coverage.months_with_trades == 1
    assert coverage.empty_months == months[1:]
    assert coverage.trailing_blackout_days == days
