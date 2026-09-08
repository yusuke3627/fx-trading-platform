"""The day's realized P&L, summed from the broker's deal history.

Balance moves for reasons other than trading: a deposit, a withdrawal or a
credit grant shifts it exactly as a closed position does, so a day's balance
change is not that day's trading result. Deals carry the distinction balance
does not — funding is booked under its own deal type — which is why the figure
is summed from the deals rather than differenced from the balance.

Realized is defined here as profit + commission + swap over the BUY/SELL deals
alone. A broker that books its fees as separate COMMISSION or INTEREST deals
therefore leaves those costs outside this figure; OANDA prices FX in the
spread, and no fee-deal account has been observed against this series.

Bounds and deal timestamps are both on the broker's wall clock, so the caller
converts the real instants it means once instead of converting every deal
back.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading.execution.mt5.mapper import broker_time_from_epoch, is_trade_deal


def realized_pnl_between(
    raw_deals: Iterable[Any], *, start: datetime, end: datetime
) -> Decimal:
    """Trading result over `[start, end]`, both bounds on the broker's clock.

    `raw_deals` are history_deals_get() results; anything that is not a
    BUY/SELL execution is dropped, which is what keeps funding out.
    """
    total = Decimal(0)
    for raw in raw_deals:
        if not is_trade_deal(raw):
            continue
        broker_time = broker_time_from_epoch(raw.time)
        if start <= broker_time <= end:
            total += (
                Decimal(str(raw.profit))
                + Decimal(str(raw.commission))
                + Decimal(str(raw.swap))
            )
    return total
