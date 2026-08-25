"""Tick -> Bar aggregation.

Every candle in this system is folded from the bid/ask tick stream: replay has
nothing else, and live bar_service reads the same stored series. copy_rates is
never called, so the two agree candle for candle by construction rather than by
matching an external grid. OHLC follows the bid series MT5 charts FX on rather
than the mid.
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
    # Broker times of the quotes that currently set open and close. Arrival
    # order is not price order, so the extremes of the bar are tracked on the
    # broker timeline rather than assumed from the order ticks came in.
    first_time: datetime
    last_time: datetime
    # Our own clock: the latest reception among the quotes folded so far. The
    # bar cannot be known before every quote in it has arrived.
    known_at: datetime


def _open_bucket(start: datetime, tick: Tick) -> _Bucket:
    return _Bucket(
        start=start,
        open=tick.bid,
        high=tick.bid,
        low=tick.bid,
        close=tick.bid,
        tick_volume=1,
        first_time=tick.time,
        last_time=tick.time,
        known_at=tick.known_time,
    )


def _fold(bucket: _Bucket, tick: Tick) -> None:
    bucket.high = max(bucket.high, tick.bid)
    bucket.low = min(bucket.low, tick.bid)
    # A quote that arrives late but timestamps earlier must not become the
    # close, and one that fills a gap at the front must become the open.
    # Quotes sharing a broker time fall back to arrival order, matching how
    # stored ticks are read back (ORDER BY event_time, id).
    if tick.time >= bucket.last_time:
        bucket.close = tick.bid
        bucket.last_time = tick.time
    if tick.time < bucket.first_time:
        bucket.open = tick.bid
        bucket.first_time = tick.time
    bucket.tick_volume += 1
    bucket.known_at = max(bucket.known_at, tick.known_time)


class BarBuilder:
    """Folds the ticks of one (symbol, timeframe) into completed bars.

    Only closed bars are published. The high, low and close of a running
    bucket are not final, so handing one to a strategy would show it a candle
    the market has not printed yet. There is deliberately no flush(): an
    unfinished bucket has no completed bar to give.

    Buckets are floored on the epoch grid of tick.time, and tick.time is the
    trade server's own wall clock labelled UTC (ADR-005). That grid is
    therefore already the server's: a 1d bucket opens at the server's midnight
    and a 4h bucket at its 00:00 / 04:00 / 08:00 ..., the boundaries the
    terminal draws. There is no session anchor to configure, and nothing here
    depends on how large the server's offset is or on when DST moves it,
    because broker timestamps are never converted into our own time.
    """

    def __init__(self, symbol: str, timeframe: str) -> None:
        self._symbol = symbol
        self._timeframe = timeframe
        self._seconds = TIMEFRAME_SECONDS[timeframe]
        self._bucket: _Bucket | None = None

    def on_tick(self, tick: Tick) -> Bar | None:
        """Fold one tick, returning the previous bar if this tick closed it.

        Which bucket a quote joins, and when a bucket is over, are both read
        from the broker clock (tick.time). When the finished bar becomes
        VISIBLE is a separate question that known_at answers on our own clock.
        The two are never compared: the broker's zone is not ours, and mixing
        them stalls the builder outright under a constant offset (ADR-005).
        """
        start = self._bucket_start(tick.time)
        bucket = self._bucket

        # Fold before publishing: a quote timestamped exactly at the close
        # belongs to the next bar, not to the one it releases.
        if bucket is not None and start == bucket.start:
            _fold(bucket, tick)

        # A broker timestamp at or past the open bar's end is proof that the
        # bar is over: no later quote can still belong to it.
        completed: Bar | None = None
        if bucket is not None and tick.time >= self._end_of(bucket):
            completed = self._to_bar(bucket, tick.known_time)
            self._bucket = bucket = None

        if bucket is None and (completed is None or completed.start < start):
            # Reopening the bucket just published would rewrite a candle a
            # strategy may already have traded on.
            self._bucket = _open_bucket(start, tick)

        # Anything else is a straggler whose bucket closed before it arrived.
        # It stays in the tick series either way.
        return completed

    def _end_of(self, bucket: _Bucket) -> datetime:
        return bucket.start + timedelta(seconds=self._seconds)

    def _bucket_start(self, at: datetime) -> datetime:
        epoch = int(at.timestamp())
        return datetime.fromtimestamp(epoch - epoch % self._seconds, tz=UTC)

    def _to_bar(self, bucket: _Bucket, closing_known_at: datetime) -> Bar:
        return Bar(
            symbol=self._symbol,
            timeframe=self._timeframe,
            start=bucket.start,
            open=bucket.open,
            high=bucket.high,
            low=bucket.low,
            close=bucket.close,
            tick_volume=bucket.tick_volume,
            # Complete only once its own quotes have arrived AND a later one
            # has proved no more are coming.
            known_at=max(bucket.known_at, closing_known_at),
        )
