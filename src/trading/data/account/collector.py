"""Account snapshot collection from MT5 into the point-in-time store.

Every loss limit is measured against this series. The JST-day baseline, the
rolling 24h baseline and the high-water mark all come from `account_snapshots`
(risk/limits.py), and `_baseline_equity` falls back to the earliest snapshot it
can see when the window's start has none — so a gap in the series does not
disable a limit, it moves the baseline and reports a different loss than the
real one. The series has to be kept, not merely available.

Daily realized P&L is summed from MT5 trade deals rather than inferred from a
balance change, so deposits and withdrawals do not become trading results.

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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from trading.backtest.clock import Clock, SystemClock
from trading.data.account.realized_pnl import realized_pnl_between
from trading.data.cli import poll_interval
from trading.data.market.dukascopy import known_to_broker_label
from trading.domain.account import AccountSnapshot
from trading.execution.mt5.adapter import (
    BROKER_TIME_MARGIN,
    MT5ConnectionError,
    load_mt5_module,
)
from trading.execution.mt5.mapper import account_key_from_info
from trading.risk.limits import jst_day_start
from trading.storage.repository import AccountSnapshotRepository

DEFAULT_INTERVAL_SECONDS = 60.0

# Published MT5 success code, duplicated like the constants in
# execution/mt5/mapper.py so the module stays testable off Windows.
RES_S_OK = 1


def build_snapshot(
    info: Any,
    *,
    observed_at: datetime,
    previous: AccountSnapshot | None,
    realized_pnl_day: Decimal,
) -> AccountSnapshot:
    """One observation of the account, placed against what came before it.

    `previous` carries the high-water mark forward from the stored series.
    """
    balance = _money(info.balance)
    equity = _money(info.equity)
    margin = _money(info.margin)
    # The high-water mark is the highest equity ever recorded, so it survives
    # every drawdown; taking the max of the window being read would let it
    # decay as old rows age out and quietly forgive the drawdown.
    high_water_mark = max(previous.high_water_mark, equity) if previous else equity
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
        realized_pnl_day=realized_pnl_day,
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
        server_ahead_of_ny_hours: float,
        clock: Clock | None = None,
        mt5_module: Any | None = None,
    ) -> None:
        self._repository = repository
        self._server_ahead_of_ny = timedelta(hours=server_ahead_of_ny_hours)
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
        snapshot = build_snapshot(
            info,
            observed_at=now,
            previous=self._repository.latest_known_before(account_id, now),
            realized_pnl_day=self._realized_pnl_day(jst_day_start(now), now),
        )
        self._repository.insert(account_id, snapshot)
        return snapshot

    def _realized_pnl_day(self, day_start: datetime, now: datetime) -> Decimal:
        start = known_to_broker_label(day_start, self._server_ahead_of_ny)
        end = known_to_broker_label(now, self._server_ahead_of_ny)
        raw = self._mt5.history_deals_get(
            start - BROKER_TIME_MARGIN, end + BROKER_TIME_MARGIN
        )
        # 執行系の adapter は MT5 の None を無条件で失敗として扱う。あちらは取得失敗を
        # 「建玉が無い」と読むと exit が生きた建玉を飛ばすからで、ここは害の向きが逆になる
        # ―― 監視用の系列なので、約定の無い日に落ちれば系列そのものが欠測する。空区間に
        # None と空タプルのどちらが返るかは Windows 実機でしか確かめられず、issue #130 で
        # 追跡している。
        if raw is None:
            code, description = self._mt5.last_error()
            if code != RES_S_OK:
                raise MT5ConnectionError(
                    f"history_deals_get failed: ({code}, {description})"
                )
            raw = ()
        return realized_pnl_between(raw, start=start, end=end)

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

    collector = AccountSnapshotCollector(
        PostgresAccountSnapshotRepository(connect(dsn)),
        server_ahead_of_ny_hours=config.market.broker_server_ahead_of_ny_hours,
    )
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
