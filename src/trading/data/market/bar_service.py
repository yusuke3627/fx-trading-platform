"""Bar generation from the stored tick series.

Bars are folded from ticks that are already persisted, never from the
collector's poll stream: polling structurally misses quotes that arrive
between two calls, and only the stored series — repaired by backfill — is
complete. Reading ticks back also carries their received_at, which is what a
bar's known_at is made of (ADR-005).

Each pass rebuilds forward from the end of the last stored bar with a fresh
builder, so the process holds no state that a restart could lose and a
re-run writes nothing new. `ON CONFLICT (symbol, timeframe, start_at) DO
NOTHING` makes the write idempotent; that also means a bar written wrong can
never be corrected, which is why a cold start drops its first candle rather
than risk persisting one whose bucket it entered halfway.

**What market_bars is.** These rows are the LIVE series: each candle as it
could be known in real time, from the ticks that had actually arrived when it
closed. A backfill that later fills a gap does NOT reach back and correct
them, and that is deliberate — a bar a strategy has already traded on must not
be rewritten underneath it, which is the same rule BarBuilder enforces when it
refuses to reopen a published bucket.

Research and replay do not read these rows at all: BacktestEngine folds bars
from the stored ticks on the fly, so a repaired gap flows into every later
replay automatically. market_ticks is the durable series; market_bars is the
record of what was knowable at the time. Anything wanting corrected candles
should rebuild from ticks rather than read here.

Usage (trading host):

    python -m trading.data.market.bar_service --env demo --symbol USDJPY
    python -m trading.data.market.bar_service --env demo --once
"""
from __future__ import annotations

import argparse
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from trading.backtest.clock import Clock, SystemClock
from trading.data.market.bars import BarBuilder, is_foldable
from trading.domain.market import TIMEFRAME_SECONDS, Bar
from trading.storage.repository import MarketBarRepository, MarketTickRepository

if TYPE_CHECKING:
    from trading.config import AppConfig

# How far back a first run reaches when no bar has ever been stored. That
# first pass reads the whole window in one query, so the bound is what keeps a
# cold start from materialising the table; every later pass only reads from
# the last stored bar and is small.
COLD_START_LOOKBACK = timedelta(days=7)

DEFAULT_INTERVAL_SECONDS = 10.0


class BarService:
    def __init__(
        self,
        ticks: MarketTickRepository,
        bars: MarketBarRepository,
        clock: Clock | None = None,
    ) -> None:
        self._ticks = ticks
        self._bars = bars
        self._clock = clock or SystemClock()

    def build_once(self, symbol: str, timeframe: str) -> int:
        """Fold every tick after the last stored bar and persist what closed.

        Returns the number of bars actually written.
        """
        now = self._clock.now()
        stored = self._bars.known_before(symbol, timeframe, now, 1)
        if stored:
            # close_time is the next bucket's start, so the fold begins on a
            # boundary and no candle is entered halfway.
            since = stored[-1].close_time
            drop_first = False
        else:
            since = now - COLD_START_LOOKBACK
            drop_first = True

        builder = BarBuilder(symbol, timeframe)
        completed: list[Bar] = []
        for tick in self._ticks.known_before(symbol, now, since):
            bar = builder.on_tick(tick)
            if bar is not None:
                completed.append(bar)

        if drop_first and completed:
            completed = completed[1:]
        return self._bars.insert_many(completed) if completed else 0

    def run(self, symbol: str, timeframes: list[str], interval_seconds: float) -> None:
        while True:
            for timeframe in timeframes:
                self.build_once(symbol, timeframe)
            time.sleep(interval_seconds)


def foldable_timeframes(configured: list[str]) -> tuple[list[str], list[str]]:
    """Split configured timeframes into the ones ticks can fold and the rest.

    4h and 1d hang off the trade server's session anchor, which BarBuilder
    refuses rather than approximate (issue #11). They are reported, not
    silently dropped: a strategy configured for one of them would otherwise
    wait forever for bars nobody is building.
    """
    foldable, refused = [], []
    for timeframe in sorted(set(configured), key=lambda tf: TIMEFRAME_SECONDS[tf]):
        (foldable if is_foldable(timeframe) else refused).append(timeframe)
    return foldable, refused


def configured_timeframes(config: AppConfig, symbol: str) -> list[str]:
    """Every timeframe any strategy declares for this symbol.

    Disabled strategies count: bars are shared data, and building them only
    once a strategy is switched on would leave it without history.
    """
    timeframes: set[str] = set()
    for strategy in config.strategies.values():
        if symbol in strategy.instruments:
            timeframes.update(strategy.timeframes.all())
    return sorted(timeframes, key=lambda tf: TIMEFRAME_SECONDS[tf])


def main() -> None:
    import os

    from trading.config import load_config

    parser = argparse.ArgumentParser(description="Bar generation from stored ticks")
    parser.add_argument("--env", default="demo")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--once",
        action="store_true",
        help="build one pass and exit instead of following the tick series",
    )
    args = parser.parse_args()

    config = load_config(args.env)
    symbol = args.symbol or config.market.primary_instruments[0]
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    timeframes, refused = foldable_timeframes(configured_timeframes(config, symbol))
    if refused:
        print(f"not folded from ticks (session-anchored, see issue #11): {refused}")
    if not timeframes:
        raise SystemExit(f"no foldable timeframe configured for {symbol}")
    print(f"building {timeframes} for {symbol}")

    # Imported here so the module stays unit-testable without the db extra.
    from trading.storage.postgres import (
        PostgresMarketBarRepository,
        PostgresMarketTickRepository,
        connect,
    )

    conn = connect(dsn)
    service = BarService(
        PostgresMarketTickRepository(conn), PostgresMarketBarRepository(conn)
    )
    if args.once:
        for timeframe in timeframes:
            print(f"{timeframe}: stored {service.build_once(symbol, timeframe)} bars")
    else:
        service.run(symbol, timeframes, args.interval_seconds)


if __name__ == "__main__":
    main()
