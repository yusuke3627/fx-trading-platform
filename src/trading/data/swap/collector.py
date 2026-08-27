"""Swap/rollover snapshot collector from MT5 into the point-in-time store.

MT5 symbol properties の swap 部分（swap_mode / swap_long / swap_short /
曜日別 rollover 倍率）を定期観測し、parsed 行（swap_snapshots）と raw
payload（events）の両方を保存する。backtest の carry 計上はこの系列の
latest-known snapshot を読む（ADR-016）。

account collector と同じ原則: 観測プロセスに執行能力は持たせない（mt5
module 注入）。broker 失敗は retry せず raise し、ホストのスケジューラが
再起動する。snapshot は瞬間の観測で、欠測期間は backfill できない。

Usage (Windows host with MT5 terminal):

    python -m trading.data.swap.collector --env demo --once
    python -m trading.data.swap.collector --env demo --symbol USDJPY --once
"""
from __future__ import annotations

import argparse
import math
import time
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.backtest.clock import Clock, SystemClock
from trading.data.cli import poll_interval
from trading.data.macro.base import payload_hash
from trading.domain.event import EventEnvelope
from trading.domain.swap import SwapSnapshot
from trading.execution.mt5.adapter import MT5ConnectionError, load_mt5_module
from trading.storage.repository import EventRepository, SwapSnapshotRepository

SWAP_SNAPSHOT_RAW = "SWAP_SNAPSHOT_RAW"

# swap 値の変化は日次以下の頻度なので、tick/account より粗い既定で観測する。
DEFAULT_INTERVAL_SECONDS = 6 * 3600.0

_PER_DAY_FIELDS = (
    "swap_sunday",
    "swap_monday",
    "swap_tuesday",
    "swap_wednesday",
    "swap_thursday",
    "swap_friday",
    "swap_saturday",
)


def _json_native(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # EventEnvelope payload は NaN/Infinity を拒否する。理論値なしを表す
        # 番兵として非有限を返すフィールドがあり得るため、文字列で保全する。
        return value if math.isfinite(value) else str(value)
    return str(value)


def build_snapshot(
    symbol: str, info: Any, *, retrieved_at: datetime
) -> tuple[SwapSnapshot, EventEnvelope]:
    """symbol_info の 1 観測を parsed 行と raw イベントへ写す（純関数）。

    per-day 倍率フィールドは terminal のビルドによって存在しないことがある
    （外部 API 境界）。無いビルドでは None を保存し、carry 側が
    swap_rollover3days から倍率を導く。
    """
    payload = {key: _json_native(value) for key, value in info._asdict().items()}
    digest = payload_hash(payload)
    per_day = {
        name: int(value) if (value := getattr(info, name, None)) is not None else None
        for name in _PER_DAY_FIELDS
    }
    snapshot = SwapSnapshot(
        snapshot_id=uuid4(),
        symbol=symbol,
        swap_mode=int(info.swap_mode),
        swap_long=Decimal(str(info.swap_long)),
        swap_short=Decimal(str(info.swap_short)),
        swap_rollover3days=int(info.swap_rollover3days),
        **per_day,
        payload_hash=digest,
        retrieved_at=retrieved_at,
        known_at=retrieved_at,
    )
    event = EventEnvelope(
        event_id=uuid4(),
        event_type=SWAP_SNAPSHOT_RAW,
        source="MT5",
        source_uri=f"mt5://symbol_info/{symbol}",
        payload=payload,
        payload_hash=digest,
        retrieved_at=retrieved_at,
        known_at=retrieved_at,
    )
    return snapshot, event


class SwapSnapshotCollector:
    def __init__(
        self,
        snapshots: SwapSnapshotRepository,
        events: EventRepository,
        *,
        clock: Clock | None = None,
        mt5_module: Any | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._events = events
        self._clock = clock or SystemClock()
        self._mt5 = mt5_module if mt5_module is not None else load_mt5_module()

    def connect(self) -> None:
        if not self._mt5.initialize():
            raise MT5ConnectionError(f"mt5.initialize failed: {self._mt5.last_error()}")

    def disconnect(self) -> None:
        self._mt5.shutdown()

    def collect_once(self, symbols: list[str]) -> list[SwapSnapshot]:
        collected: list[SwapSnapshot] = []
        for symbol in symbols:
            info = self._mt5.symbol_info(symbol)
            if info is None:
                raise MT5ConnectionError(
                    f"symbol_info({symbol!r}) failed: {self._mt5.last_error()}"
                )
            snapshot, event = build_snapshot(
                symbol, info, retrieved_at=self._clock.now()
            )
            self._events.insert(event)
            self._snapshots.insert(snapshot)
            collected.append(snapshot)
        return collected

    def run(self, symbols: list[str], interval_seconds: float) -> None:
        while True:
            self.collect_once(symbols)
            time.sleep(interval_seconds)


def main() -> None:
    import os

    from trading.config import load_config

    parser = argparse.ArgumentParser(description="MT5 swap/rollover snapshot collector")
    parser.add_argument("--env", default="demo")
    parser.add_argument(
        "--symbol",
        action="append",
        default=None,
        help="broker symbol (repeatable); default: platform-enabled instruments",
    )
    parser.add_argument(
        "--interval-seconds", type=poll_interval, default=DEFAULT_INTERVAL_SECONDS
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="record one snapshot per symbol and exit",
    )
    args = parser.parse_args()

    config = load_config(args.env)
    symbols = args.symbol or sorted(
        symbol
        for symbol, policy in config.instruments.items()
        if policy.platform_enabled
    )
    if not symbols:
        raise SystemExit(
            "no symbols to collect: pass --symbol or enable instruments in config"
        )
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    # Imported here so the module stays unit-testable without the db extra.
    from trading.storage.postgres import (
        PostgresEventRepository,
        PostgresSwapSnapshotRepository,
        connect,
    )

    conn = connect(dsn)
    collector = SwapSnapshotCollector(
        PostgresSwapSnapshotRepository(conn), PostgresEventRepository(conn)
    )
    collector.connect()
    try:
        if args.once:
            for snapshot in collector.collect_once(symbols):
                print(
                    f"{snapshot.symbol}: mode={snapshot.swap_mode} "
                    f"long={snapshot.swap_long} short={snapshot.swap_short} "
                    f"rollover3days={snapshot.swap_rollover3days}"
                )
        else:
            collector.run(symbols, args.interval_seconds)
    finally:
        collector.disconnect()


if __name__ == "__main__":
    main()
