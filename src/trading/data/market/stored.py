"""MarketDataService over the persisted series.

The live counterpart of InMemoryMarketData: the same reads, answered from
market_ticks and market_bars instead of memory. Live and replay therefore
differ only in where the rows come from, not in what a strategy is allowed to
see — visibility is the injected clock in both.

Instrument specs are passed in as a snapshot taken at startup, not looked up
through a broker adapter. A strategy reaches its instrument through this
service, and holding an adapter here would put the broker one attribute away
from the strategy layer.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta

from trading.backtest.clock import Clock
from trading.domain.instrument import InstrumentSpec
from trading.domain.market import Bar, Tick
from trading.storage.repository import MarketBarRepository, MarketTickRepository


class StoredMarketData:
    def __init__(
        self,
        ticks: MarketTickRepository,
        bars: MarketBarRepository,
        clock: Clock,
        instruments: Mapping[str, InstrumentSpec],
    ) -> None:
        self._ticks = ticks
        self._bars = bars
        self._clock = clock
        self._instruments = dict(instruments)

    def bars(self, symbol: str, timeframe: str, count: int) -> Sequence[Bar]:
        return self._bars.known_before(symbol, timeframe, self._clock.now(), count)

    def ticks(self, symbol: str, window_seconds: float) -> Sequence[Tick]:
        # The window is anchored on the newest visible quote rather than on
        # now(): event_time lives on the broker's clock, which is offset from
        # ours (ADR-005), and a stale or quiet market would otherwise return an
        # empty window while a price is perfectly well known.
        now = self._clock.now()
        latest = self._ticks.latest_known_before(symbol, now)
        if latest is None:
            return []
        return self._ticks.known_before(
            symbol, now, latest.time - timedelta(seconds=window_seconds)
        )

    def latest_tick(self, symbol: str) -> Tick | None:
        return self._ticks.latest_known_before(symbol, self._clock.now())

    def instrument(self, symbol: str) -> InstrumentSpec | None:
        return self._instruments.get(symbol)
