"""MarketDataService for the replay engine's feed order.

InMemoryMarketData re-filters its whole history on every read because
arbitrary preloads may hold data the clock has not reached. The engine never
produces that shape: it advances the clock to a tick's known time BEFORE
adding it, and a bar is added at the instant of the tick that closed it, so
everything stored here is already visible. That contract makes every read
O(window) instead of O(history) — the difference between a recorded scalp
week replaying in minutes and never finishing (reads per tick times a scan
of all ticks so far is quadratic). A late-received quote whose broker time
is older than the newest is handled by add_tick keeping the window sorted.

Only the engine may feed this class. Anything preloading history the clock
has not reached needs InMemoryMarketData and its visibility filter.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import timedelta

from trading.domain.instrument import InstrumentSpec
from trading.domain.market import Bar, Tick

# Far above any strategy's configured tick window (minutes); bounds what a
# long replay holds in the window store on top of the engine's own dataset.
TICK_HORIZON_SECONDS = 3600.0

# Bars retained per (symbol, timeframe): a week of 1m bars, decades of 1d —
# orders of magnitude above any indicator window, while a months-long 1m
# replay stays bounded instead of accumulating the whole period.
BAR_CAPACITY = 10_000


class ReplayMarketData:
    def __init__(
        self,
        tick_horizon_seconds: float = TICK_HORIZON_SECONDS,
        bar_capacity: int = BAR_CAPACITY,
    ) -> None:
        self._horizon = timedelta(seconds=tick_horizon_seconds)
        self._bar_capacity = bar_capacity
        self._ticks: dict[str, deque[Tick]] = {}
        self._bars: dict[tuple[str, str], list[Bar]] = {}
        self._instruments: dict[str, InstrumentSpec] = {}

    def add_tick(self, tick: Tick) -> None:
        window = self._ticks.setdefault(tick.symbol, deque())
        # The engine feeds in known-time order, which is broker-time order
        # except when a late-received old quote replays. Inserting that one
        # back at its broker position keeps the deque sorted on (time,
        # arrival) — the stored series' (event_time, id) order — so the
        # window scan stays a tail read and latest_tick is never rewound to
        # the late old price. The scan is O(1) for the in-order case and
        # reaches only as far back as the quote is late.
        position = len(window)
        while position > 0 and window[position - 1].time > tick.time:
            position -= 1
        if position == len(window):
            window.append(tick)
        else:
            window.insert(position, tick)
        cutoff = window[-1].time - self._horizon
        while window and window[0].time < cutoff:
            window.popleft()

    def add_bar(self, bar: Bar) -> None:
        window = self._bars.setdefault((bar.symbol, bar.timeframe), [])
        window.append(bar)
        # Trimmed in blocks so the append stays amortized O(1) and the tail
        # slice below stays a plain list slice.
        if len(window) > 2 * self._bar_capacity:
            del window[: len(window) - self._bar_capacity]

    def set_instrument(self, spec: InstrumentSpec) -> None:
        self._instruments[spec.symbol] = spec

    def bars(self, symbol: str, timeframe: str, count: int) -> Sequence[Bar]:
        if count > self._bar_capacity:
            # Silent truncation would hand an indicator less history than it
            # asked for and let it compute on a starved window.
            raise ValueError(
                f"bar window {count} exceeds the retained capacity "
                f"{self._bar_capacity}"
            )
        return self._bars.get((symbol, timeframe), [])[-count:]

    def ticks(self, symbol: str, window_seconds: float) -> Sequence[Tick]:
        if window_seconds > self._horizon.total_seconds():
            # Silent truncation would hand the strategy a shorter window than
            # it asked for and let it read a spike where there was none.
            raise ValueError(
                f"tick window {window_seconds}s exceeds the retained horizon "
                f"{self._horizon.total_seconds()}s"
            )
        window = self._ticks.get(symbol)
        if not window:
            return []
        start = window[-1].time - timedelta(seconds=window_seconds)
        recent: list[Tick] = []
        for tick in reversed(window):
            if tick.time < start:
                break
            recent.append(tick)
        recent.reverse()
        return recent

    def latest_tick(self, symbol: str) -> Tick | None:
        window = self._ticks.get(symbol)
        return window[-1] if window else None

    def instrument(self, symbol: str) -> InstrumentSpec | None:
        return self._instruments.get(symbol)
