"""Market data service.

Live implementation reads from MT5 / persisted ticks; the in-memory
implementation backs tests and replay.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

from trading.backtest.clock import Clock
from trading.domain.instrument import InstrumentSpec
from trading.domain.market import Bar, Tick


class MarketDataService(Protocol):
    def bars(self, symbol: str, timeframe: str, count: int) -> Sequence[Bar]: ...

    def ticks(self, symbol: str, window_seconds: float) -> Sequence[Tick]: ...

    def latest_tick(self, symbol: str) -> Tick | None: ...

    def instrument(self, symbol: str) -> InstrumentSpec | None: ...


class InMemoryMarketData:
    """In-memory store for tests and replay.

    Replay MUST construct this with the ReplayClock: pre-loaded history is
    then filtered so only data already known at clock.now() is visible (bars
    by close_time, ticks by known_time = received_at or broker time) —
    without the clock a strategy could read future highs/lows/closes straight
    past the replay engine. Omitting the clock is for live/incremental
    feeding only.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock
        self._ticks: dict[str, list[Tick]] = {}
        self._bars: dict[tuple[str, str], list[Bar]] = {}
        self._instruments: dict[str, InstrumentSpec] = {}

    def add_tick(self, tick: Tick) -> None:
        self._ticks.setdefault(tick.symbol, []).append(tick)

    def add_bar(self, bar: Bar) -> None:
        self._bars.setdefault((bar.symbol, bar.timeframe), []).append(bar)

    def set_instrument(self, spec: InstrumentSpec) -> None:
        self._instruments[spec.symbol] = spec

    def bars(self, symbol: str, timeframe: str, count: int) -> Sequence[Bar]:
        bars = self._bars.get((symbol, timeframe), [])
        if self._clock is not None:
            now = self._clock.now()
            bars = [b for b in bars if b.close_time <= now]
        return bars[-count:]

    def ticks(self, symbol: str, window_seconds: float) -> Sequence[Tick]:
        ticks = self._visible_ticks(symbol)
        if not ticks:
            return []
        # Arrival order is not price order once late ticks exist: the window
        # is anchored and sorted on the broker timeline.
        end = max(t.time for t in ticks)
        start = end - timedelta(seconds=window_seconds)
        return sorted((t for t in ticks if t.time >= start), key=lambda t: t.time)

    def latest_tick(self, symbol: str) -> Tick | None:
        # A late-arriving old tick must never rewind the latest price: latest
        # is by broker time among visible ticks, not by arrival.
        ticks = self._visible_ticks(symbol)
        return max(ticks, key=lambda t: t.time) if ticks else None

    def instrument(self, symbol: str) -> InstrumentSpec | None:
        return self._instruments.get(symbol)

    def _visible_ticks(self, symbol: str) -> list[Tick]:
        ticks = self._ticks.get(symbol, [])
        if self._clock is None:
            return ticks
        now = self._clock.now()
        # Visibility follows the reception time: a late-arriving tick was not
        # usable at its broker timestamp.
        return [t for t in ticks if t.known_time <= now]
