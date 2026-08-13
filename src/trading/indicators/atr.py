"""Average True Range (Wilder)."""
from __future__ import annotations

from typing import Sequence

from trading.domain.market import Bar


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    out: list[float] = []
    prev_close: float | None = None
    for b in bars:
        high, low = float(b.high), float(b.low)
        if prev_close is None:
            out.append(high - low)
        else:
            out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = float(b.close)
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Wilder-smoothed ATR of the last bar; None when there is too little data."""
    if period <= 0:
        raise ValueError("period must be positive")
    trs = true_ranges(bars)
    if len(trs) < period + 1:
        return None
    value = sum(trs[1 : period + 1]) / period
    for tr in trs[period + 1 :]:
        value = (value * (period - 1) + tr) / period
    return value
