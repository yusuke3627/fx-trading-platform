"""Shared indicator layer.

Strategies consume indicators through IndicatorService; they never own
indicator implementations. Duplicating an indicator inside a strategy is
forbidden because Backtest/Live and cross-strategy results would diverge.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta

from trading.data.market import MarketDataService
from trading.data.market.clock import broker_label_to_known
from trading.domain.market import Bar, Tick
from trading.indicators import market_structure as ms
from trading.indicators.atr import atr as _atr
from trading.indicators.ema import ema as _ema
from trading.indicators.momentum import rate_of_change, tick_momentum
from trading.indicators.session import Session, session_start
from trading.indicators.volatility import realized_volatility as _rvol
from trading.indicators.vwap import vwap as _vwap

DEFAULT_BAR_COUNT = 200


class IndicatorService:
    def __init__(
        self, market: MarketDataService, bar_count: int = DEFAULT_BAR_COUNT,
        *, broker_server_ahead_of_ny_hours: float = 7.0,
    ) -> None:
        self._market = market
        self._bar_count = bar_count
        self._server_ahead_of_ny = timedelta(hours=broker_server_ahead_of_ny_hours)
        self._cache: dict[
            tuple[str, str, str, int], tuple[tuple[Bar, ...], float | None]
        ] = {}

    def _memoized(
        self,
        name: str,
        symbol: str,
        timeframe: str,
        param: int,
        count: int,
        compute: Callable[[tuple[Bar, ...]], float | None],
    ) -> float | None:
        bars = tuple(self._market.bars(symbol, timeframe, count))
        key = (name, symbol, timeframe, param)
        cached = self._cache.get(key)
        # 末尾の時刻だけでは過去足の訂正を見逃すため、入力窓全体を比較する。
        if cached is None or cached[0] != bars:
            cached = (bars, compute(bars))
            self._cache[key] = cached
        return cached[1]

    def atr(self, symbol: str, timeframe: str, period: int = 14) -> float | None:
        # The read follows the requested period: a configured period beyond
        # the default window must widen the read, not silently starve the
        # indicator into a permanent None.
        count = max(self._bar_count, period + 1)
        return self._memoized(
            "atr", symbol, timeframe, period, count, lambda bars: _atr(bars, period),
        )

    def ema(self, symbol: str, timeframe: str, period: int) -> float | None:
        count = max(self._bar_count, period + 1)
        return self._memoized(
            "ema", symbol, timeframe, period, count,
            lambda bars: _ema([float(b.close) for b in bars], period),
        )

    def vwap(
        self,
        symbol: str,
        timeframe: str = "1m",
        session: Session | None = None,
    ) -> float | None:
        bars = list(self._market.bars(symbol, timeframe, self._bar_count))
        if session is not None and bars:
            # Session anchor is derived from the latest bar time (data-driven,
            # no wall clock) so replay and live agree.
            instants = [broker_label_to_known(b.start, self._server_ahead_of_ny) for b in bars]
            start = session_start(session, instants[-1])
            bars = [b for b, instant in zip(bars, instants) if instant >= start]
        return _vwap(bars)

    def momentum(self, symbol: str, timeframe: str, lookback: int) -> float | None:
        return self._memoized(
            "momentum", symbol, timeframe, lookback, self._bar_count,
            lambda bars: rate_of_change([float(b.close) for b in bars], lookback),
        )

    def tick_momentum(
        self, symbol: str, window_seconds: float, *, ticks: Sequence[Tick] | None = None,
    ) -> float | None:
        if ticks is None:
            ticks = self._market.ticks(symbol, window_seconds)
        return tick_momentum(ticks, window_seconds)

    def realized_volatility(self, symbol: str, timeframe: str, window: int) -> float | None:
        return self._memoized(
            "realized_volatility", symbol, timeframe, window, self._bar_count,
            lambda bars: _rvol([float(b.close) for b in bars], window),
        )

    def recent_high(self, symbol: str, timeframe: str, lookback: int) -> float | None:
        return self._memoized(
            "recent_high", symbol, timeframe, lookback, self._bar_count,
            lambda bars: ms.rolling_high(bars, lookback),
        )

    def recent_low(self, symbol: str, timeframe: str, lookback: int) -> float | None:
        return self._memoized(
            "recent_low", symbol, timeframe, lookback, self._bar_count,
            lambda bars: ms.rolling_low(bars, lookback),
        )
