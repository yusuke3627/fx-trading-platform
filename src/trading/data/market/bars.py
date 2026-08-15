"""Tick -> Bar aggregation.

Live MT5 serves bars directly (copy_rates); replay has only the bid/ask tick
stream, so bars are folded from ticks here. The two must agree bar for bar,
which is why OHLC follows the bid series MT5 charts FX on rather than the mid.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.domain.market import TIMEFRAME_SECONDS, Bar, Tick


@dataclass
class _Bucket:
    """The bar currently being folded. Private and mutable by design: it is
    not a Bar until it closes."""

    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int


def _open_bucket(start: datetime, tick: Tick) -> _Bucket:
    return _Bucket(
        start=start,
        open=tick.bid,
        high=tick.bid,
        low=tick.bid,
        close=tick.bid,
        tick_volume=1,
    )


class BarBuilder:
    """Folds the ticks of one (symbol, timeframe) into completed bars.

    Only closed bars are published. The high, low and close of a running
    bucket are not final, so handing one to a strategy would show it a candle
    the market has not printed yet. There is deliberately no flush(): an
    unfinished bucket has no completed bar to give.
    """

    def __init__(self, symbol: str, timeframe: str) -> None:
        self._symbol = symbol
        self._timeframe = timeframe
        self._seconds = TIMEFRAME_SECONDS[timeframe]
        self._bucket: _Bucket | None = None

    def on_tick(self, tick: Tick) -> Bar | None:
        """Fold one tick, returning the previous bar if this tick closed it.

        Bucketing follows the broker timeline (tick.time); when the bar
        becomes VISIBLE is a separate concern, enforced downstream by
        close_time against the replay clock.
        """
        start = self._bucket_start(tick.time)
        if tick.known_time > start + timedelta(seconds=self._seconds):
            # Reached us only after its own bar had closed. A bar is what was
            # knowable by its close — and it is persisted with
            # known_at = close_time — so folding this quote in would let a
            # later replay read the bar before its contents existed.
            return None
        bucket = self._bucket
        if bucket is None:
            self._bucket = _open_bucket(start, tick)
            return None
        if start < bucket.start:
            # A late arrival for a bucket that already closed. Replay delivers
            # in reception order, so this is normal after a reconnect —
            # rewriting a published bar would change a candle a strategy may
            # already have traded on.
            return None
        if start == bucket.start:
            bucket.high = max(bucket.high, tick.bid)
            bucket.low = min(bucket.low, tick.bid)
            bucket.close = tick.bid
            bucket.tick_volume += 1
            return None
        self._bucket = _open_bucket(start, tick)
        return self._to_bar(bucket)

    def _bucket_start(self, at: datetime) -> datetime:
        epoch = int(at.timestamp())
        return datetime.fromtimestamp(epoch - epoch % self._seconds, tz=UTC)

    def _to_bar(self, bucket: _Bucket) -> Bar:
        return Bar(
            symbol=self._symbol,
            timeframe=self._timeframe,
            start=bucket.start,
            open=bucket.open,
            high=bucket.high,
            low=bucket.low,
            close=bucket.close,
            tick_volume=bucket.tick_volume,
        )
