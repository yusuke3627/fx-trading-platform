"""Tick -> Bar aggregation.

Live MT5 serves bars directly (copy_rates); replay has only the bid/ask tick
stream, so bars are folded from ticks here. The two have to agree candle for
candle, which drives two decisions: OHLC follows the bid series MT5 charts FX
on rather than the mid, and only timeframes whose grid is independent of the
broker's session anchor are built at all (see BarBuilder).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.domain.market import TIMEFRAME_SECONDS, Bar, Tick

SECONDS_PER_HOUR = 3600


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


class BarBuilder:
    """Folds the ticks of one (symbol, timeframe) into completed bars.

    Only closed bars are published. The high, low and close of a running
    bucket are not final, so handing one to a strategy would show it a candle
    the market has not printed yet. There is deliberately no flush(): an
    unfinished bucket has no completed bar to give.

    Buckets sit on a UTC grid. That matches the broker for any timeframe
    dividing one hour, because MT5 trade servers are offset from UTC by whole
    hours; 4h and 1d candles instead hang off the server's own midnight, so
    building them here would silently disagree with copy_rates. Those
    timeframes are rejected rather than approximated - the anchor has to come
    from the broker before they can be folded from ticks.
    """

    def __init__(self, symbol: str, timeframe: str) -> None:
        seconds = TIMEFRAME_SECONDS[timeframe]
        if SECONDS_PER_HOUR % seconds != 0:
            raise ValueError(
                f"{timeframe} bars cannot be folded from ticks yet: their boundaries "
                "follow the broker's session anchor, which the platform does not know. "
                "Timeframes dividing one hour are anchor-independent and are supported."
            )
        self._symbol = symbol
        self._timeframe = timeframe
        self._seconds = seconds
        self._bucket: _Bucket | None = None

    def on_tick(self, tick: Tick) -> Bar | None:
        """Fold one tick, returning the previous bar if this tick closed it.

        Bucketing follows the broker timeline (tick.time); when the bar
        becomes VISIBLE is a separate concern, enforced downstream by
        close_time against the replay clock.
        """
        start = self._bucket_start(tick.time)
        # A quote that reached us only after its own bar had closed cannot be
        # part of it: bars are persisted with known_at = close_time, so
        # folding it in would let a later replay read the candle before its
        # contents existed. Reception exactly at the close still counts —
        # visibility is `<=`.
        joins = tick.known_time <= start + timedelta(seconds=self._seconds)
        bucket = self._bucket

        # Fold before publishing: a quote known exactly at the close belongs
        # to the very bar it closes.
        if bucket is not None and joins and start == bucket.start:
            _fold(bucket, tick)

        # Any reception at or past the open bar's end is proof that the bar is
        # over, whichever bucket the tick itself belongs to — so a straggler
        # or a future-dated quote releases a finished candle without joining
        # it, instead of leaving it withheld until some later tick.
        completed: Bar | None = None
        if bucket is not None and tick.known_time >= self._end_of(bucket):
            completed = self._to_bar(bucket)
            self._bucket = bucket = None

        if joins and bucket is None and (completed is None or completed.start < start):
            # Reopening the bucket just published would rewrite a candle a
            # strategy may already have traded on.
            self._bucket = _open_bucket(start, tick)

        # Anything else sits outside the open bar: its bucket closed before it
        # (a straggler), or a broker clock running ahead of ours put it in a
        # later one that cannot be opened yet without losing the bar in
        # progress. It stays in the tick series either way.
        return completed

    def _end_of(self, bucket: _Bucket) -> datetime:
        return bucket.start + timedelta(seconds=self._seconds)

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
