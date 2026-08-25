"""Shared indicator layer.

Strategies consume indicators through IndicatorService; they never own
indicator implementations. Duplicating an indicator inside a strategy is
forbidden because Backtest/Live and cross-strategy results would diverge.
"""
from __future__ import annotations

from trading.data.market import MarketDataService
from trading.indicators import market_structure as ms
from trading.indicators.atr import atr as _atr
from trading.indicators.ema import ema as _ema
from trading.indicators.momentum import rate_of_change, tick_momentum
from trading.indicators.session import Session, session_start
from trading.indicators.volatility import realized_volatility as _rvol
from trading.indicators.vwap import vwap as _vwap

DEFAULT_BAR_COUNT = 200


class IndicatorService:
    def __init__(self, market: MarketDataService, bar_count: int = DEFAULT_BAR_COUNT) -> None:
        self._market = market
        self._bar_count = bar_count

    def atr(self, symbol: str, timeframe: str, period: int = 14) -> float | None:
        # The read follows the requested period: a configured period beyond
        # the default window must widen the read, not silently starve the
        # indicator into a permanent None.
        count = max(self._bar_count, period + 1)
        return _atr(self._market.bars(symbol, timeframe, count), period)

    def ema(self, symbol: str, timeframe: str, period: int) -> float | None:
        count = max(self._bar_count, period + 1)
        closes = [float(b.close) for b in self._market.bars(symbol, timeframe, count)]
        return _ema(closes, period)

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
            start = session_start(session, bars[-1].start)
            bars = [b for b in bars if b.start >= start]
        return _vwap(bars)

    def momentum(self, symbol: str, timeframe: str, lookback: int) -> float | None:
        closes = [float(b.close) for b in self._market.bars(symbol, timeframe, self._bar_count)]
        return rate_of_change(closes, lookback)

    def tick_momentum(self, symbol: str, window_seconds: float) -> float | None:
        return tick_momentum(self._market.ticks(symbol, window_seconds), window_seconds)

    def realized_volatility(self, symbol: str, timeframe: str, window: int) -> float | None:
        closes = [float(b.close) for b in self._market.bars(symbol, timeframe, self._bar_count)]
        return _rvol(closes, window)

    def recent_high(self, symbol: str, timeframe: str, lookback: int) -> float | None:
        return ms.rolling_high(self._market.bars(symbol, timeframe, self._bar_count), lookback)

    def recent_low(self, symbol: str, timeframe: str, lookback: int) -> float | None:
        return ms.rolling_low(self._market.bars(symbol, timeframe, self._bar_count), lookback)
