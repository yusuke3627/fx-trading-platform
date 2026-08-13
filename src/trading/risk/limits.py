"""Loss measurement over account snapshots.

Three complementary views so a JST date rollover cannot reset a drawdown:
JST calendar day, rolling 24 hours, and high-water-mark drawdown.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from trading.domain.account import AccountSnapshot

JST = timezone(timedelta(hours=9))


def jst_day_start(now: datetime) -> datetime:
    """UTC instant of 00:00 JST for the JST day containing `now`."""
    local = now.astimezone(JST)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.astimezone(UTC)


def _equity_at_or_before(
    snapshots: Sequence[AccountSnapshot], t: datetime
) -> Decimal | None:
    eligible = [s for s in snapshots if s.observed_at <= t]
    if not eligible:
        return None
    return max(eligible, key=lambda s: s.observed_at).equity


def _loss_pct(baseline: Decimal | None, current: Decimal) -> Decimal:
    if baseline is None or baseline <= 0:
        return Decimal(0)
    loss = (baseline - current) / baseline * Decimal(100)
    return max(loss, Decimal(0))


def daily_loss_pct(
    snapshots: Sequence[AccountSnapshot], current: AccountSnapshot, now: datetime
) -> Decimal:
    """Equity loss since the start of the current JST day, in percent."""
    baseline = _equity_at_or_before(snapshots, jst_day_start(now))
    return _loss_pct(baseline, current.equity)


def rolling_24h_loss_pct(
    snapshots: Sequence[AccountSnapshot], current: AccountSnapshot, now: datetime
) -> Decimal:
    """Equity loss versus 24 hours ago, in percent."""
    baseline = _equity_at_or_before(snapshots, now - timedelta(hours=24))
    return _loss_pct(baseline, current.equity)


def hwm_drawdown_pct(current: AccountSnapshot) -> Decimal:
    """Drawdown from the recorded high-water mark, in percent."""
    return _loss_pct(current.high_water_mark, current.equity)
