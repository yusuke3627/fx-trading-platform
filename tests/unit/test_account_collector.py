"""Account snapshots: the high-water mark carries forward, the JST day does not."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.support import T0, FakeAccountSnapshotRepository, FixedClock, at, make_snapshot
from trading.data.account.collector import AccountSnapshotCollector, build_snapshot
from trading.execution.mt5.adapter import MT5ConnectionError

# T0 is 09:00 JST, so the JST day it belongs to opened nine hours earlier.
JST_DAY_START = at(hours=-9)


DEMO_LOGIN = 10000001
LIVE_LOGIN = 20000002


def account_info(
    balance: str = "1000000",
    equity: str = "1000000",
    margin: str = "0",
    margin_level: str = "0",
    profit: str = "0",
    login: int = DEMO_LOGIN,
) -> SimpleNamespace:
    # MT5 hands back floats; the collector is what turns them into Decimal.
    return SimpleNamespace(
        login=login,
        balance=float(balance),
        equity=float(equity),
        margin=float(margin),
        margin_free=float(equity),
        margin_level=float(margin_level),
        profit=float(profit),
    )


class FakeMt5:
    def __init__(self, info: SimpleNamespace | None = None) -> None:
        self.info = info if info is not None else account_info()
        self.initialized = False
        self.was_shut_down = False

    def initialize(self) -> bool:
        self.initialized = True
        return True

    def shutdown(self) -> None:
        self.was_shut_down = True

    def account_info(self) -> SimpleNamespace | None:
        return self.info

    def last_error(self) -> tuple[int, str]:
        return (-1, "fake terminal")


def snapshot_of(info: SimpleNamespace, *, previous=None, day_baseline=None):
    return build_snapshot(
        info, observed_at=T0, previous=previous, day_baseline=day_baseline
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


def test_the_day_result_is_the_balance_move_since_the_days_first_snapshot():
    opening = make_snapshot("1000000", observed_at=at(hours=-8), balance="1000000")

    snapshot = snapshot_of(
        account_info(balance="1000500", equity="1002000"),
        previous=opening,
        day_baseline=opening,
    )

    assert snapshot.realized_pnl_day == Decimal(500)
    # The open book's swing belongs to unrealized, not to the day's result.
    assert snapshot.unrealized_pnl == Decimal(0)


def test_the_day_result_starts_from_zero_when_the_jst_day_has_no_snapshot_yet():
    # Yesterday's rows are not a baseline for today: the JST rollover resets
    # what "the day's result" means. The mark is not reset by it — it belongs to
    # the account's whole history, not to a calendar day.
    yesterday = make_snapshot(
        "1000000", observed_at=at(hours=-10), balance="900000", high_water_mark="1050000"
    )

    snapshot = snapshot_of(
        account_info(balance="1000500"), previous=yesterday, day_baseline=None
    )

    assert snapshot.realized_pnl_day == Decimal(0)
    assert snapshot.high_water_mark == Decimal(1050000)


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
        repository, clock=FixedClock(T0), mt5_module=terminal
    )

    snapshot = collector.collect_once()

    assert repository.snapshots == [(str(DEMO_LOGIN), snapshot)]
    assert snapshot.observed_at == T0
    assert snapshot.broker_connected is True


def test_switching_the_terminal_to_another_account_starts_a_new_series():
    # A demo run and a live run against the same database must not share a
    # high-water mark. The equities are unrelated, so a drawdown measured
    # across them is not a drawdown — it is the gap between two accounts.
    repository = FakeAccountSnapshotRepository()
    terminal = FakeMt5(account_info(equity="1000000", login=DEMO_LOGIN))
    clock = FixedClock(T0)
    collector = AccountSnapshotCollector(repository, clock=clock, mt5_module=terminal)
    collector.collect_once()

    clock.advance(minutes=1)
    terminal.info = account_info(equity="50000", login=LIVE_LOGIN)
    switched = collector.collect_once()

    assert switched.high_water_mark == Decimal(50000)
    assert switched.drawdown_from_hwm == Decimal(0)


def test_successive_collections_carry_the_mark_and_the_day_forward():
    repository = FakeAccountSnapshotRepository()
    terminal = FakeMt5(account_info(balance="1000000", equity="1010000"))
    clock = FixedClock(JST_DAY_START)
    collector = AccountSnapshotCollector(repository, clock=clock, mt5_module=terminal)
    collector.collect_once()

    clock.advance(hours=5)
    terminal.info = account_info(balance="1005000", equity="1002000")
    second = collector.collect_once()

    assert second.high_water_mark == Decimal(1010000)
    assert second.drawdown_from_hwm == Decimal(8000)
    assert second.realized_pnl_day == Decimal(5000)


def test_a_terminal_that_reports_no_account_raises():
    terminal = FakeMt5()
    terminal.info = None
    collector = AccountSnapshotCollector(
        FakeAccountSnapshotRepository(), clock=FixedClock(T0), mt5_module=terminal
    )

    with pytest.raises(MT5ConnectionError):
        collector.collect_once()


def test_connect_and_disconnect_drive_the_terminal():
    terminal = FakeMt5()
    collector = AccountSnapshotCollector(
        FakeAccountSnapshotRepository(), clock=FixedClock(T0), mt5_module=terminal
    )

    collector.connect()
    collector.disconnect()

    assert terminal.initialized
    assert terminal.was_shut_down
