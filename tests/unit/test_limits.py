from datetime import UTC, datetime
from decimal import Decimal

from tests.support import make_snapshot
from trading.risk.limits import (
    daily_loss_pct,
    hwm_drawdown_pct,
    jst_day_start,
    rolling_24h_loss_pct,
)


def test_jst_day_start_is_15_utc_previous_day():
    now = datetime(2026, 8, 13, 3, 0, tzinfo=UTC)  # 12:00 JST
    assert jst_day_start(now) == datetime(2026, 8, 12, 15, 0, tzinfo=UTC)


def test_daily_loss_measured_from_jst_day_start():
    now = datetime(2026, 8, 13, 3, 0, tzinfo=UTC)
    snapshots = [
        make_snapshot("1000000", observed_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC)),
    ]
    current = make_snapshot("992500", observed_at=now)
    assert daily_loss_pct(snapshots, current, now) == Decimal("0.75")


def test_rolling_24h_catches_midnight_straddle():
    # -0.70% before JST midnight and -0.70% after: the daily window resets,
    # the rolling 24h window does not.
    now = datetime(2026, 8, 13, 15, 10, tzinfo=UTC)  # 00:10 JST on Aug 14
    day_start = jst_day_start(now)  # Aug 13 15:00 UTC
    snapshots = [
        make_snapshot("1000000", observed_at=now.replace(hour=10, day=12)),
        make_snapshot("993000", observed_at=day_start),
    ]
    current = make_snapshot("986000", observed_at=now)

    daily = daily_loss_pct(snapshots, current, now)
    rolling = rolling_24h_loss_pct(snapshots, current, now)
    assert daily < Decimal("0.75")
    assert rolling >= Decimal("1.00")


def test_hwm_drawdown():
    current = make_snapshot("1000000", high_water_mark="1050000")
    drawdown = hwm_drawdown_pct(current)
    assert Decimal("4.76") < drawdown < Decimal("4.77")


def test_missing_baseline_falls_back_to_earliest_snapshot():
    # First day of operation: no snapshot exists at/before the JST day start,
    # so the earliest available snapshot becomes the baseline instead of
    # silently reporting 0% loss.
    now = datetime(2026, 8, 13, 3, 0, tzinfo=UTC)
    snapshots = [
        make_snapshot("1000000", observed_at=datetime(2026, 8, 12, 20, 0, tzinfo=UTC)),
    ]
    current = make_snapshot("992500", observed_at=now)
    assert daily_loss_pct(snapshots, current, now) == Decimal("0.75")


def test_no_baseline_means_zero_loss():
    now = datetime(2026, 8, 13, 3, 0, tzinfo=UTC)
    current = make_snapshot("990000", observed_at=now)
    assert daily_loss_pct([], current, now) == Decimal(0)
