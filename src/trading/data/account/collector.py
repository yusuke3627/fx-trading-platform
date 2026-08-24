"""Account snapshot collection from MT5 into the point-in-time store.

Every loss limit is measured against this series. The JST-day baseline, the
rolling 24h baseline and the high-water mark all come from `account_snapshots`
(risk/limits.py), and `_baseline_equity` falls back to the earliest snapshot it
can see when the window's start has none — so a gap in the series does not
disable a limit, it moves the baseline and reports a different loss than the
real one. The series has to be kept, not merely available.

The MT5 module is injected rather than the execution adapter, exactly as the
tick collector does it: an object able to send orders has no business inside a
process whose only job is to observe.

A broker failure raises instead of being retried. The process exits, the host's
scheduler restarts it, and the missed period stays missing — snapshots are
observations of a moment and cannot be backfilled after it.

Usage (Windows host with MT5 terminal):

    python -m trading.data.account.collector --env demo
    python -m trading.data.account.collector --env demo --once
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading.backtest.clock import Clock, SystemClock
from trading.data.cli import poll_interval
from trading.domain.account import AccountSnapshot
from trading.execution.mt5.adapter import MT5ConnectionError, load_mt5_module
from trading.execution.mt5.mapper import account_key_from_info
from trading.risk.limits import jst_day_start
from trading.storage.repository import AccountSnapshotRepository

DEFAULT_INTERVAL_SECONDS = 60.0


def build_snapshot(
    info: Any,
    *,
    observed_at: datetime,
    previous: AccountSnapshot | None,
    day_baseline: AccountSnapshot | None,
) -> AccountSnapshot:
    """One observation of the account, placed against what came before it.

    `previous` carries the high-water mark forward and `day_baseline` fixes
    where the JST day started; both come from the stored series rather than
    from the terminal, which knows neither.
    """
    balance = _money(info.balance)
    equity = _money(info.equity)
    margin = _money(info.margin)
    # The high-water mark is the highest equity ever recorded, so it survives
    # every drawdown; taking the max of the window being read would let it
    # decay as old rows age out and quietly forgive the drawdown.
    high_water_mark = max(previous.high_water_mark, equity) if previous else equity
    # The day's balance move since its first snapshot. Trading is not the only
    # thing that moves balance — a deposit or a withdrawal moves it too, and
    # this figure cannot tell them apart, so on a day with a funding
    # transaction it is not the trading result (issue #36). Risk does not read
    # it: every limit is measured on equity.
    day_open_balance = day_baseline.balance if day_baseline else balance
    return AccountSnapshot(
        observed_at=observed_at,
        balance=balance,
        equity=equity,
        margin=margin,
        free_margin=_money(info.margin_free),
        # MT5 reports 0 when nothing is committed to margin. That is "no ratio
        # to report", not a margin level of zero, and the two would grade very
        # differently.
        margin_level=_money(info.margin_level) if margin > 0 else None,
        unrealized_pnl=_money(info.profit),
        realized_pnl_day=balance - day_open_balance,
        high_water_mark=high_water_mark,
        drawdown_from_hwm=max(high_water_mark - equity, Decimal(0)),
        broker_connected=True,
    )


def _money(value: Any) -> Decimal:
    return Decimal(str(value))


class AccountSnapshotCollector:
    def __init__(
        self,
        repository: AccountSnapshotRepository,
        *,
        clock: Clock | None = None,
        mt5_module: Any | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or SystemClock()
        self._mt5 = mt5_module if mt5_module is not None else load_mt5_module()

    def connect(self) -> None:
        if not self._mt5.initialize():
            raise MT5ConnectionError(f"mt5.initialize failed: {self._mt5.last_error()}")

    def disconnect(self) -> None:
        self._mt5.shutdown()

    def collect_once(self) -> AccountSnapshot:
        info = self._mt5.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info failed: {self._mt5.last_error()}")
        account_id = account_key_from_info(info)
        now = self._clock.now()
        today = self._repository.since(account_id, jst_day_start(now))
        snapshot = build_snapshot(
            info,
            observed_at=now,
            previous=self._repository.latest(account_id),
            day_baseline=today[0] if today else None,
        )
        self._repository.insert(account_id, snapshot)
        return snapshot

    def run(self, interval_seconds: float) -> None:
        while True:
            self.collect_once()
            time.sleep(interval_seconds)


def main() -> None:
    import os

    from trading.config import load_config

    parser = argparse.ArgumentParser(description="MT5 account snapshot collector")
    parser.add_argument("--env", default="demo")
    parser.add_argument(
        "--interval-seconds", type=poll_interval, default=DEFAULT_INTERVAL_SECONDS
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="record one snapshot and exit instead of following the account",
    )
    args = parser.parse_args()

    config = load_config(args.env)
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    # Imported here so the module stays unit-testable without the db extra.
    from trading.storage.postgres import PostgresAccountSnapshotRepository, connect

    collector = AccountSnapshotCollector(PostgresAccountSnapshotRepository(connect(dsn)))
    collector.connect()
    try:
        if args.once:
            snapshot = collector.collect_once()
            print(
                f"equity={snapshot.equity} balance={snapshot.balance} "
                f"hwm={snapshot.high_water_mark} day_pnl={snapshot.realized_pnl_day}"
            )
        else:
            collector.run(args.interval_seconds)
    finally:
        collector.disconnect()


if __name__ == "__main__":
    main()
