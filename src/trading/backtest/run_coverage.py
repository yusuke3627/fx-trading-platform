"""Coverage of entry times in closed trade records."""
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class RunCoverage:
    first_trade_at: datetime | None
    last_trade_at: datetime | None
    months_with_trades: int
    months_in_period: int
    empty_months: tuple[str, ...]
    trailing_blackout_days: float | None


def run_coverage(
    entry_ats: Iterable[datetime], period_from: datetime, period_to: datetime
) -> RunCoverage:
    first_trade_at = None
    last_trade_at = None
    trade_months: set[str] = set()
    for at in entry_ats:
        trade_months.add(f"{at:%Y-%m}")
        if first_trade_at is None or at < first_trade_at:
            first_trade_at = at
        if last_trade_at is None or at > last_trade_at:
            last_trade_at = at

    period_months: list[str] = []
    month = period_from.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month < period_to:
        period_months.append(f"{month:%Y-%m}")
        if month.month == 12:
            month = month.replace(year=month.year + 1, month=1)
        else:
            month = month.replace(month=month.month + 1)

    return RunCoverage(
        first_trade_at=first_trade_at,
        last_trade_at=last_trade_at,
        months_with_trades=len(trade_months),
        months_in_period=len(period_months),
        empty_months=tuple(month for month in period_months if month not in trade_months),
        trailing_blackout_days=(
            (period_to - last_trade_at) / timedelta(days=1)
            if last_trade_at is not None else None
        ),
    )
