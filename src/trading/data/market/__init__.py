"""Market data service.

Live implementation reads from MT5 / persisted ticks; the in-memory
implementation backs tests and replay.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

from trading.domain.instrument import InstrumentSpec
from trading.domain.market import Bar, Tick


class MarketDataService(Protocol):
    def bars(self, symbol: str, timeframe: str, count: int) -> Sequence[Bar]: ...

    def ticks(self, symbol: str, window_seconds: float) -> Sequence[Tick]: ...

    def latest_tick(self, symbol: str) -> Tick | None: ...

    def instrument(self, symbol: str) -> InstrumentSpec | None: ...


class InMemoryMarketData:
    def __init__(self) -> None:
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
        return self._bars.get((symbol, timeframe), [])[-count:]

    def ticks(self, symbol: str, window_seconds: float) -> Sequence[Tick]:
        ticks = self._ticks.get(symbol, [])
        if not ticks:
            return []
        end = ticks[-1].time
        start = end - timedelta(seconds=window_seconds)
        return [t for t in ticks if t.time >= start]

    def latest_tick(self, symbol: str) -> Tick | None:
        ticks = self._ticks.get(symbol, [])
        return ticks[-1] if ticks else None

    def instrument(self, symbol: str) -> InstrumentSpec | None:
        return self._instruments.get(symbol)
