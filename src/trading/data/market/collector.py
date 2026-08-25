"""USD/JPY tick collection from MT5 into the point-in-time store.

OANDA's published historical ticks are gated behind volume conditions, so the
bid/ask series scalp research and execution calibration need is collected
first-hand from the trading host.

Two paths, deliberately different in purpose:

- Polling `symbol_info_tick` keeps the latest quote flowing. It returns only
  the newest tick, so quotes arriving between two polls are structurally
  missed; the polling series is never a complete tick record.
- `copy_ticks_range` backfills a period after the fact and is what makes the
  stored series complete. Scheduled backfill runs, not the poll loop, are how
  a full research dataset is produced.

A broker fetch failure raises instead of being retried here. Reconnect
policy, gap detection and retry intervals are operational decisions that are
not fixed in code: the process exits, the host's scheduler restarts it, and
the period it missed is repaired with a backfill run.

Usage (Windows host with MT5 terminal):

    python -m trading.data.market.collector --env demo --symbol USDJPY
    python -m trading.data.market.collector --env demo --symbol USDJPY \
        --backfill-from 2026-08-14T00:00:00Z --backfill-to 2026-08-15T00:00:00Z
"""
from __future__ import annotations

import argparse
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID, uuid4

from trading.backtest.clock import Clock, SystemClock
from trading.data.cli import aware_utc, poll_interval
from trading.domain.market import Tick
from trading.execution.mt5.adapter import MT5ConnectionError, load_mt5_module
from trading.storage.repository import MarketTickRepository

# Published MT5 constant, duplicated like the ones in execution/mt5/mapper.py
# so the collector stays testable off Windows.
COPY_TICKS_ALL = -1

INSERT_CHUNK_SIZE = 10_000

# copy_ticks_range materializes the whole range at once, and an active USD/JPY
# day is 10^5-10^6 ticks; a multi-day request is split so the terminal never
# has to hand back a week in one array.
BACKFILL_WINDOW = timedelta(days=1)

SOURCE_MT5 = "MT5"

_T = TypeVar("_T")


def _utc_from_msc(time_msc: int) -> datetime:
    """Millisecond broker timestamp -> aware UTC.

    time_msc, not time: at second resolution repeated quotes inside one second
    collapse under UNIQUE (symbol, event_time, bid, ask), which is exactly the
    intra-second bid/ask movement scalp research is about.

    The epoch is taken as UTC without correcting for the broker's server
    offset, matching how execution/mt5/mapper.py timestamps deals; correcting
    it only here would put fills and ticks on different timelines. Since every
    row also stores received_at, any real offset is measurable from the stored
    data rather than assumed.
    """
    return datetime.fromtimestamp(time_msc / 1000, tz=UTC)


def tick_from_info(raw: Any, symbol: str, received_at: datetime) -> Tick:
    """Map a symbol_info_tick result (attribute access)."""
    return Tick(
        symbol=symbol,
        bid=Decimal(str(raw.bid)),
        ask=Decimal(str(raw.ask)),
        time=_utc_from_msc(int(raw.time_msc)),
        received_at=received_at,
    )


def tick_from_row(row: Any, symbol: str, received_at: datetime) -> Tick:
    """Map one copy_ticks_range row.

    The range call returns a numpy structured array whose records are
    numpy.void: fields are readable by key only, so this cannot share the
    attribute-based mapping above.
    """
    return Tick(
        symbol=symbol,
        bid=Decimal(str(row["bid"])),
        ask=Decimal(str(row["ask"])),
        time=_utc_from_msc(int(row["time_msc"])),
        received_at=received_at,
    )


def _windows(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    while start < end:
        window_end = min(start + BACKFILL_WINDOW, end)
        yield start, window_end
        start = window_end


def _chunks(items: Sequence[_T]) -> Iterator[Sequence[_T]]:
    for offset in range(0, len(items), INSERT_CHUNK_SIZE):
        yield items[offset : offset + INSERT_CHUNK_SIZE]


class TickCollector:
    """Collects ticks into the PIT store.

    The MT5 module is injected so the mapping and polling logic runs off
    Windows against a fake. Only the six market-data calls are used: the
    execution adapter is deliberately not reused, since holding an object that
    can send orders would put an order path inside the collection process.
    Its loader and connection error are shared, though — a second exception
    type for the same terminal failure would only force callers to catch both.
    """

    def __init__(
        self,
        repository: MarketTickRepository,
        *,
        clock: Clock | None = None,
        mt5_module: Any | None = None,
        source: str = SOURCE_MT5,
    ) -> None:
        self._repository = repository
        self._clock = clock or SystemClock()
        self._mt5 = mt5_module if mt5_module is not None else load_mt5_module()
        self._source = source
        # One run per process, so the rows a single execution wrote can be
        # identified and removed afterwards.
        self._ingestion_run: UUID = uuid4()
        self._last_quote: dict[str, tuple[datetime, Decimal, Decimal]] = {}

    def connect(self, symbol: str) -> None:
        if not self._mt5.initialize():
            raise MT5ConnectionError(f"mt5.initialize failed: {self._mt5.last_error()}")
        # A symbol absent from Market Watch yields no tick at all, so
        # selection is part of connecting rather than an optimization.
        if not self._mt5.symbol_select(symbol, True):
            raise MT5ConnectionError(
                f"symbol_select({symbol}) failed: {self._mt5.last_error()}"
            )

    def disconnect(self) -> None:
        self._mt5.shutdown()

    def poll_once(self, symbol: str) -> int:
        """Fetch the current quote and store it. Returns rows actually added."""
        raw = self._mt5.symbol_info_tick(symbol)
        if raw is None:
            # A failed fetch is not "no tick": treating an outage as an empty
            # feed would silently leave a hole in the series.
            raise MT5ConnectionError(
                f"symbol_info_tick({symbol}) failed: {self._mt5.last_error()}"
            )
        tick = tick_from_info(raw, symbol, self._clock.now())
        # Polling re-reads the same quote until the broker publishes a new
        # one. Skipping the repeat only saves the round trip; the unique key
        # is what actually keeps the table free of duplicates. Keyed by
        # symbol so the check still holds when several are polled in turn.
        quote = (tick.time, tick.bid, tick.ask)
        if self._last_quote.get(symbol) == quote:
            return 0
        self._last_quote[symbol] = quote
        return self._write([tick])

    def backfill(self, symbol: str, start: datetime, end: datetime) -> int:
        stored = 0
        for window_start, window_end in _windows(start, end):
            rows = self._mt5.copy_ticks_range(
                symbol, window_start, window_end, COPY_TICKS_ALL
            )
            if rows is None:
                raise MT5ConnectionError(
                    f"copy_ticks_range({symbol}, {window_start}, {window_end}) "
                    f"failed: {self._mt5.last_error()}"
                )
            received_at = self._clock.now()
            # Converted a chunk at a time: a busy day is 10^6 rows, and
            # building every Tick up front would hold the whole day as objects
            # on top of the array the terminal already returned.
            window_stored = 0
            for chunk in _chunks(rows):
                window_stored += self._write(
                    [tick_from_row(row, symbol, received_at) for row in chunk]
                )
            stored += window_stored
            # A year-long import runs for hours; a silent one is
            # indistinguishable from a hang and gets interrupted.
            print(
                f"{window_start:%Y-%m-%d}: {len(rows)} ticks, +{window_stored} new",
                flush=True,
            )
        return stored

    def run(self, symbol: str, interval_seconds: float) -> None:
        while True:
            self.poll_once(symbol)
            time.sleep(interval_seconds)

    def _write(self, ticks: Sequence[Tick]) -> int:
        return self._repository.insert_many(
            ticks, source=self._source, ingestion_run=self._ingestion_run
        )


def main() -> None:
    import os

    from trading.config import load_config

    parser = argparse.ArgumentParser(description="MT5 tick collector")
    parser.add_argument("--env", default="demo")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--interval-seconds", type=poll_interval, default=None)
    parser.add_argument("--backfill-from", type=aware_utc, default=None)
    parser.add_argument("--backfill-to", type=aware_utc, default=None)
    args = parser.parse_args()

    if (args.backfill_from is None) != (args.backfill_to is None):
        parser.error("--backfill-from and --backfill-to must be given together")
    if args.backfill_from is not None and args.backfill_from >= args.backfill_to:
        parser.error("--backfill-from must be earlier than --backfill-to")

    config = load_config(args.env)
    symbol = args.symbol or config.market.primary_instruments[0]
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    # Imported here so the module stays usable (and unit-testable) without the
    # db extra installed; psycopg is only needed once a connection is opened.
    from trading.storage.postgres import PostgresMarketTickRepository, connect

    collector = TickCollector(PostgresMarketTickRepository(connect(dsn)))
    collector.connect(symbol)
    try:
        if args.backfill_from is not None:
            stored = collector.backfill(symbol, args.backfill_from, args.backfill_to)
            print(f"backfill stored {stored} ticks")
        else:
            interval = (
                args.interval_seconds
                if args.interval_seconds is not None
                else config.market.tick_poll_interval_seconds
            )
            collector.run(symbol, interval)
    finally:
        collector.disconnect()


if __name__ == "__main__":
    main()
