"""Bar generation from the stored tick series.

Bars are folded from ticks that are already persisted, never from the
collector's poll stream: polling structurally misses quotes that arrive
between two calls, and only the stored series — repaired by backfill — is
complete. Reading ticks back also carries their received_at, which is what a
bar's known_at is made of (ADR-005).

Each pass rebuilds forward from the end of the last stored bar with a fresh
builder, so the process holds no state that a restart could lose and a
re-run writes nothing new. A pass whose bucket has not ended yet stops at two
index seeks, so holding no state does not mean re-reading the day at the poll
rate. `ON CONFLICT (symbol, timeframe, start_at) DO
NOTHING` makes the write idempotent; that also means a bar written wrong can
never be corrected, which is why a first pass begins at the first bucket
boundary its read fully covers rather than persisting a candle assembled from
part of one.

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

**Backfilling the archive.** A host that starts folding bars long after its
ticks were backfilled has none of that history, and a strategy whose slowest
window is fifty daily candles cannot run at all until it does — the live
passes only ever build forward. `--backfill` folds the whole stored tick
series in one read, for the spans no live pass covered. It writes only where
no row exists, on the same ON CONFLICT that makes a re-run a no-op, so a
candle a strategy already traded on is never touched; and known_at stays
honest, because these candles became knowable when their ticks were ingested
rather than when the market printed them.

Usage (trading host):

    python -m trading.data.market.bar_service --env demo --symbol USDJPY
    python -m trading.data.market.bar_service --env demo --once
    python -m trading.data.market.bar_service --env demo --backfill
"""
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, TextIO

from trading.backtest.clock import Clock, SystemClock
from trading.data.cli import poll_interval
from trading.data.market.bars import BarBuilder, bucket_start
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

# How many candles of one timeframe wait in memory before they are written.
# A backfill of years produces enough 1m rows to matter; the long timeframes
# reach the end of the read holding a handful.
BACKFILL_BATCH_BARS = 2_000

# How far the broker's clock may run ahead of ours (ADR-005): the read's end
# bound is a broker timestamp, and the anchor is at most a few hours.
BROKER_CLOCK_MARGIN = timedelta(days=1)


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
        else:
            since = self._cold_start(symbol, timeframe, now)
            if since is None:
                return 0

        if not self._a_bucket_has_closed(symbol, timeframe, since, now):
            return 0

        builder = BarBuilder(symbol, timeframe)
        completed: list[Bar] = []
        for tick in self._ticks.known_before(symbol, now, since):
            bar = builder.on_tick(tick)
            if bar is not None:
                completed.append(bar)

        return self._bars.insert_many(completed) if completed else 0

    def backfill(
        self,
        symbol: str,
        timeframes: Sequence[str],
        progress: TextIO | None = None,
    ) -> dict[str, int]:
        """Fold the stored tick series into candles in one read.

        Every timeframe rides the same pass: the archive is tens of millions
        of quotes, and reading it once per timeframe would multiply hours of
        work to produce rows a single fold already has in hand.

        A pass that dies partway is restarted from the beginning, and there is
        deliberately no way to resume from a later point. The timeframes do
        not reach the same depth at the same moment — a minute candle is
        written every few thousand bars while two years of daily ones are
        still in hand — so a shared resume point would skip the long
        timeframes over everything before it and report a complete run with
        years missing from exactly the windows this exists to fill. Restarting
        costs the read again; the idempotent write is what makes it correct.

        The bucket the first quote falls into is given up unless the quote
        opens it, for the reason a cold start gives one up: a candle missing
        its first minutes looks like any other once written, and ON CONFLICT
        leaves no way to correct it.
        """
        now = self._clock.now()
        # Bounds are the broker's clock, which runs ahead of ours (ADR-005),
        # so a real-UTC `now` would cut the newest quotes out of the read.
        end = now + BROKER_CLOCK_MARGIN
        # No stored quote predates the epoch, so this reads as "from the
        # beginning of the series" without a query for where that is.
        start = datetime(1970, 1, 1, tzinfo=UTC)
        bounds = self._ticks.bounds_between(symbol, start, end)
        if bounds is None:
            return {timeframe: 0 for timeframe in timeframes}
        first, _ = bounds

        builders = {tf: BarBuilder(symbol, tf) for tf in timeframes}
        opens_at = {tf: self._first_full_bucket(first.time, tf) for tf in timeframes}
        pending: dict[str, list[Bar]] = {tf: [] for tf in timeframes}
        written = {tf: 0 for tf in timeframes}
        day: date | None = None

        for count, tick in enumerate(
            self._ticks.stream_between(symbol, start, end), start=1
        ):
            for timeframe, builder in builders.items():
                bar = builder.on_tick(tick)
                if bar is None or bar.start < opens_at[timeframe]:
                    continue
                pending[timeframe].append(bar)
                if len(pending[timeframe]) >= BACKFILL_BATCH_BARS:
                    written[timeframe] += self._bars.insert_many(pending[timeframe])
                    pending[timeframe].clear()
            if progress is not None and tick.time.date() != day:
                day = tick.time.date()
                folded = ", ".join(
                    f"{tf}:{written[tf] + len(pending[tf])}" for tf in timeframes
                )
                print(f"{day} {count:>12,} ticks  {folded}", file=progress, flush=True)

        for timeframe, bars in pending.items():
            written[timeframe] += self._bars.insert_many(bars)
        return written

    @staticmethod
    def _first_full_bucket(at: datetime, timeframe: str) -> datetime:
        opened = bucket_start(at, timeframe)
        if at == opened:
            return opened
        return opened + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])

    def _cold_start(self, symbol: str, timeframe: str, now: datetime) -> datetime | None:
        """Where a first pass begins folding, or None with nothing to read.

        The bucket the oldest readable quote falls in can be cut from either
        side — by the lookback bound, or by the start of the series itself when
        collection began partway through it. Neither cut shows in the candle
        that comes out: an OHLC missing its morning looks like any other, and
        ON CONFLICT DO NOTHING means it could never be corrected. Beginning at
        the next boundary gives that bucket up instead.

        Skipping it is also what lets the pass make progress. Folding it and
        discarding the result afterwards would leave the resume point where it
        was, so the next pass would re-read the same span to discard the same
        candle again — at 1d, until a second bucket closes.
        """
        first = self._ticks.earliest_known_after(
            symbol, now, now - COLD_START_LOOKBACK
        )
        if first is None:
            return None
        start = bucket_start(first.time, timeframe)
        if first.time == start:
            return start
        return start + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])

    def _a_bucket_has_closed(
        self, symbol: str, timeframe: str, since: datetime, now: datetime
    ) -> bool:
        """Whether folding from `since` could produce anything.

        Two index seeks in place of the window read. A pass rebuilds from the
        last stored bar, so without this check the long timeframes re-read
        their whole span every interval to publish nothing until the boundary
        — for 1d that is the trading day so far, growing all day, at the poll
        rate.

        Which bucket is pending is decided by the first unfolded quote, not by
        `since`: after a market closure `since` sits on an empty stretch the
        series skipped, and the bucket actually waiting is the one that opens
        when quoting resumes.
        """
        first = self._ticks.earliest_known_after(symbol, now, since)
        if first is None:
            return False
        end = bucket_start(first.time, timeframe) + timedelta(
            seconds=TIMEFRAME_SECONDS[timeframe]
        )
        # The same proof BarBuilder needs: a broker timestamp past the bucket's
        # end, which no later quote can fall back behind.
        return self._ticks.earliest_known_after(symbol, now, end) is not None

    def run(self, symbol: str, timeframes: list[str], interval_seconds: float) -> None:
        while True:
            for timeframe in timeframes:
                self.build_once(symbol, timeframe)
            time.sleep(interval_seconds)


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
    parser.add_argument(
        "--interval-seconds", type=poll_interval, default=DEFAULT_INTERVAL_SECONDS
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="build one pass and exit instead of following the tick series",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="fold the whole stored tick series once, for the spans the live "
        "passes never covered, and exit",
    )
    args = parser.parse_args()

    config = load_config(args.env)
    symbol = args.symbol or config.market.primary_instruments[0]
    dsn = os.environ.get(config.storage.dsn_env)
    if not dsn:
        raise SystemExit(f"{config.storage.dsn_env} is not set")

    timeframes = configured_timeframes(config, symbol)
    if not timeframes:
        raise SystemExit(f"no timeframe configured for {symbol}")
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
    if args.backfill:
        # Progress goes to stderr: the read is hours long, and a run that
        # says nothing cannot be told from one that is stuck.
        written = service.backfill(symbol, timeframes, sys.stderr)
        for timeframe in timeframes:
            print(f"{timeframe}: stored {written[timeframe]} bars")
    elif args.once:
        for timeframe in timeframes:
            print(f"{timeframe}: stored {service.build_once(symbol, timeframe)} bars")
    else:
        service.run(symbol, timeframes, args.interval_seconds)


if __name__ == "__main__":
    main()
