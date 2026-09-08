"""Account snapshots: the high-water mark carries forward, the JST day does not."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.support import T0, FakeAccountSnapshotRepository, FixedClock, at, make_snapshot
from trading.data.account.collector import (
    RES_S_OK,
    AccountSnapshotCollector,
    build_snapshot,
)
from trading.data.market.dukascopy import known_to_broker_label
from trading.execution.mt5.adapter import MT5ConnectionError

# T0 is 09:00 JST, so the JST day it belongs to opened nine hours earlier.
JST_DAY_START = at(hours=-9)
SERVER_AHEAD_OF_NY_HOURS = 7.0
SERVER_AHEAD_OF_NY = timedelta(hours=SERVER_AHEAD_OF_NY_HOURS)


DEMO_LOGIN = 10000001
LIVE_LOGIN = 20000002
DEMO_SERVER = "Test-Broker Demo"
LIVE_SERVER = "Test-Broker Live"


def account_info(
    balance: str = "1000000",
    equity: str = "1000000",
    margin: str = "0",
    margin_level: str = "0",
    profit: str = "0",
    login: int = DEMO_LOGIN,
    server: str = DEMO_SERVER,
) -> SimpleNamespace:
    # MT5 hands back floats; the collector is what turns them into Decimal.
    return SimpleNamespace(
        login=login,
        server=server,
        balance=float(balance),
        equity=float(equity),
        margin=float(margin),
        margin_free=float(equity),
        margin_level=float(margin_level),
        profit=float(profit),
    )


class FakeMt5:
    def __init__(
        self,
        info: SimpleNamespace | None = None,
        *,
        deals: tuple[SimpleNamespace, ...] = (),
        history_deals_none: bool = False,
        error: tuple[int, str] = (-1, "fake terminal"),
    ) -> None:
        self.info = info if info is not None else account_info()
        self.deals = deals
        self.history_deals_none = history_deals_none
        self.error = error
        self.initialized = False
        self.was_shut_down = False

    def initialize(self) -> bool:
        self.initialized = True
        return True

    def shutdown(self) -> None:
        self.was_shut_down = True

    def account_info(self) -> SimpleNamespace | None:
        return self.info

    def history_deals_get(
        self, date_from, date_to
    ) -> tuple[SimpleNamespace, ...] | None:
        if self.history_deals_none:
            return None
        return tuple(
            deal
            for deal in self.deals
            if date_from.timestamp() <= deal.time <= date_to.timestamp()
        )

    def last_error(self) -> tuple[int, str]:
        return self.error


def snapshot_of(
    info: SimpleNamespace, *, previous=None, realized_pnl_day=Decimal(0)
):
    return build_snapshot(
        info,
        observed_at=T0,
        previous=previous,
        realized_pnl_day=realized_pnl_day,
    )


def test_the_first_snapshot_marks_its_own_equity_as_the_high_water_mark():
    snapshot = snapshot_of(account_info(equity="1000000"))

    assert snapshot.high_water_mark == Decimal(1000000)
    assert snapshot.drawdown_from_hwm == Decimal(0)


def test_the_high_water_mark_follows_equity_upwards():
    previous = make_snapshot("1000000", observed_at=at(hours=-1))

    snapshot = snapshot_of(account_info(equity="1010000"), previous=previous)

    assert snapshot.high_water_mark == Decimal(1010000)
    assert snapshot.drawdown_from_hwm == Decimal(0)


def test_the_high_water_mark_survives_a_drawdown():
    # The mark is the highest equity ever recorded, so a losing stretch is
    # measured against the peak rather than against a fading recent maximum.
    previous = make_snapshot("1010000", observed_at=at(hours=-1))

    snapshot = snapshot_of(account_info(equity="990000"), previous=previous)

    assert snapshot.high_water_mark == Decimal(1010000)
    assert snapshot.drawdown_from_hwm == Decimal(20000)


def test_snapshot_records_the_realized_pnl_supplied_from_deal_history():
    snapshot = snapshot_of(
        account_info(balance="1000500", equity="1002000"),
        realized_pnl_day=Decimal("-125.5"),
    )

    assert snapshot.realized_pnl_day == Decimal("-125.5")
    assert snapshot.unrealized_pnl == Decimal(0)


def test_funding_deals_are_excluded_from_the_collected_day_result():
    # The balance the terminal reports includes a 100,000 deposit, so the old
    # balance-difference figure would have booked the deposit as a win. The
    # deals say the trading result was 100.
    broker_time = known_to_broker_label(T0, SERVER_AHEAD_OF_NY).timestamp()
    terminal = FakeMt5(
        account_info(balance="1100100"),
        deals=(
            SimpleNamespace(
                type=0,
                time=broker_time,
                profit=125.0,
                commission=-5.0,
                swap=-20.0,
            ),
            SimpleNamespace(
                type=2,
                time=broker_time,
                profit=100000.0,
                commission=0.0,
                swap=0.0,
            ),
        ),
    )
    collector = AccountSnapshotCollector(
        FakeAccountSnapshotRepository(),
        server_ahead_of_ny_hours=SERVER_AHEAD_OF_NY_HOURS,
        clock=FixedClock(T0),
        mt5_module=terminal,
    )

    snapshot = collector.collect_once()

    assert snapshot.realized_pnl_day == Decimal(100)


def test_margin_level_is_absent_when_nothing_is_committed_to_margin():
    # MT5 reports 0.0 with a flat book. Recording that as a level of zero would
    # read as a margin call.
    flat = snapshot_of(account_info(margin="0", margin_level="0"))
    committed = snapshot_of(account_info(margin="50000", margin_level="2030.1"))

    assert flat.margin_level is None
    assert committed.margin_level == Decimal("2030.1")


def test_collect_once_appends_the_observation_to_the_series():
    repository = FakeAccountSnapshotRepository()
    terminal = FakeMt5(account_info(equity="1000000"))
    collector = AccountSnapshotCollector(
        repository,
        server_ahead_of_ny_hours=SERVER_AHEAD_OF_NY_HOURS,
        clock=FixedClock(T0),
        mt5_module=terminal,
    )

    snapshot = collector.collect_once()

    assert repository.snapshots == [(f"{DEMO_SERVER}:{DEMO_LOGIN}", snapshot)]
    assert snapshot.observed_at == T0
    assert snapshot.broker_connected is True


def test_switching_the_terminal_to_another_account_starts_a_new_series():
    # A demo run and a live run against the same database must not share a
    # high-water mark. The equities are unrelated, so a drawdown measured
    # across them is not a drawdown — it is the gap between two accounts.
    repository = FakeAccountSnapshotRepository()
    terminal = FakeMt5(account_info(equity="1000000", login=DEMO_LOGIN))
    clock = FixedClock(T0)
    collector = AccountSnapshotCollector(
        repository,
        server_ahead_of_ny_hours=SERVER_AHEAD_OF_NY_HOURS,
        clock=clock,
        mt5_module=terminal,
    )
    collector.collect_once()

    clock.advance(minutes=1)
    terminal.info = account_info(equity="50000", login=LIVE_LOGIN)
    switched = collector.collect_once()

    assert switched.high_water_mark == Decimal(50000)
    assert switched.drawdown_from_hwm == Decimal(0)


def test_the_same_login_on_another_server_is_another_account():
    # Login numbers are issued per server, so the same number exists on more
    # than one and is a different account on each.
    repository = FakeAccountSnapshotRepository()
    terminal = FakeMt5(account_info(equity="1000000", server=DEMO_SERVER))
    clock = FixedClock(T0)
    collector = AccountSnapshotCollector(
        repository,
        server_ahead_of_ny_hours=SERVER_AHEAD_OF_NY_HOURS,
        clock=clock,
        mt5_module=terminal,
    )
    collector.collect_once()

    clock.advance(minutes=1)
    terminal.info = account_info(equity="50000", server=LIVE_SERVER)
    switched = collector.collect_once()

    assert switched.high_water_mark == Decimal(50000)


def test_successive_collections_carry_the_mark_and_the_day_forward():
    repository = FakeAccountSnapshotRepository()
    terminal = FakeMt5(account_info(balance="1000000", equity="1010000"))
    clock = FixedClock(JST_DAY_START)
    collector = AccountSnapshotCollector(
        repository,
        server_ahead_of_ny_hours=SERVER_AHEAD_OF_NY_HOURS,
        clock=clock,
        mt5_module=terminal,
    )
    collector.collect_once()

    clock.advance(hours=5)
    terminal.info = account_info(balance="1005000", equity="1002000")
    terminal.deals = (
        SimpleNamespace(
            type=1,
            time=known_to_broker_label(
                JST_DAY_START + timedelta(hours=4), SERVER_AHEAD_OF_NY
            ).timestamp(),
            profit=5000.0,
            commission=0.0,
            swap=0.0,
        ),
    )
    second = collector.collect_once()

    assert second.high_water_mark == Decimal(1010000)
    assert second.drawdown_from_hwm == Decimal(8000)
    assert second.realized_pnl_day == Decimal(5000)


def test_a_terminal_that_reports_no_account_raises():
    terminal = FakeMt5()
    terminal.info = None
    collector = AccountSnapshotCollector(
        FakeAccountSnapshotRepository(),
        server_ahead_of_ny_hours=SERVER_AHEAD_OF_NY_HOURS,
        clock=FixedClock(T0),
        mt5_module=terminal,
    )

    with pytest.raises(MT5ConnectionError):
        collector.collect_once()


def test_history_fetch_failure_raises():
    terminal = FakeMt5(
        history_deals_none=True,
        error=(-10004, "history unavailable"),
    )
    collector = AccountSnapshotCollector(
        FakeAccountSnapshotRepository(),
        server_ahead_of_ny_hours=SERVER_AHEAD_OF_NY_HOURS,
        clock=FixedClock(T0),
        mt5_module=terminal,
    )

    with pytest.raises(
        MT5ConnectionError,
        match=r"history_deals_get failed: \(-10004, history unavailable\)",
    ):
        collector.collect_once()


def test_no_deals_with_a_success_status_records_zero():
    repository = FakeAccountSnapshotRepository()
    terminal = FakeMt5(
        history_deals_none=True,
        error=(RES_S_OK, "Success"),
    )
    collector = AccountSnapshotCollector(
        repository,
        server_ahead_of_ny_hours=SERVER_AHEAD_OF_NY_HOURS,
        clock=FixedClock(T0),
        mt5_module=terminal,
    )

    snapshot = collector.collect_once()

    assert snapshot.realized_pnl_day == Decimal(0)
    assert repository.snapshots == [(f"{DEMO_SERVER}:{DEMO_LOGIN}", snapshot)]


def test_connect_and_disconnect_drive_the_terminal():
    terminal = FakeMt5()
    collector = AccountSnapshotCollector(
        FakeAccountSnapshotRepository(),
        server_ahead_of_ny_hours=SERVER_AHEAD_OF_NY_HOURS,
        clock=FixedClock(T0),
        mt5_module=terminal,
    )

    collector.connect()
    collector.disconnect()

    assert terminal.initialized
    assert terminal.was_shut_down
