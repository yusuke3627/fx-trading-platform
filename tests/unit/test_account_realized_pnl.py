"""Realized P&L is what the trade deals say, not what the balance did."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from trading.data.account.realized_pnl import realized_pnl_between

START = datetime(2026, 8, 13, tzinfo=UTC)
END = START + timedelta(hours=8)

# MT5 deal types. Only BUY/SELL are executions; the rest move the balance
# without any position having been traded.
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
DEAL_TYPE_BALANCE = 2
DEAL_TYPE_COMMISSION = 7


def deal(
    *,
    deal_type: int = DEAL_TYPE_BUY,
    when: datetime = START,
    profit: float = 0.0,
    commission: float = 0.0,
    swap: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        type=deal_type,
        time=when.timestamp(),
        profit=profit,
        commission=commission,
        swap=swap,
    )


def test_funding_deals_are_not_included_in_realized_pnl():
    # A day with a 100,000 deposit and two small trades. Read from the balance
    # move this day looks like a 100,100 win; the trades made 100.
    raw_deals = (
        deal(deal_type=DEAL_TYPE_BUY, profit=125.0),
        deal(deal_type=DEAL_TYPE_SELL, profit=-25.0),
        deal(deal_type=DEAL_TYPE_BALANCE, profit=100000.0),
    )

    result = realized_pnl_between(raw_deals, start=START, end=END)

    assert result == Decimal(100)


def test_a_separately_booked_commission_deal_is_left_out():
    # Costs count only where the execution carries them. A broker that books
    # fees as their own deal keeps them outside this figure — recorded so the
    # limit is a decision rather than an accident.
    raw_deals = (
        deal(deal_type=DEAL_TYPE_BUY, profit=125.0),
        deal(deal_type=DEAL_TYPE_COMMISSION, profit=-30.0),
    )

    result = realized_pnl_between(raw_deals, start=START, end=END)

    assert result == Decimal(125)


def test_profit_commission_and_swap_are_all_included():
    result = realized_pnl_between(
        (deal(profit=125.5, commission=-2.25, swap=-1.5),),
        start=START,
        end=END,
    )

    assert result == Decimal("121.75")
    assert isinstance(result, Decimal)


def test_only_deals_inside_the_inclusive_window_are_included():
    raw_deals = (
        deal(when=START - timedelta(seconds=1), profit=1000.0),
        deal(when=START, profit=10.0),
        deal(when=START + timedelta(hours=4), profit=20.0),
        deal(when=END, profit=30.0),
        deal(when=END + timedelta(seconds=1), profit=2000.0),
    )

    result = realized_pnl_between(raw_deals, start=START, end=END)

    assert result == Decimal(60)


def test_no_deals_returns_zero():
    assert realized_pnl_between((), start=START, end=END) == Decimal(0)
