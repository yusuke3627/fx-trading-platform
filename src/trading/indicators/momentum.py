"""Momentum measures."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from trading.domain.market import Tick


def rate_of_change(values: Sequence[float], lookback: int) -> float | None:
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if len(values) <= lookback:
        return None
    base = values[-lookback - 1]
    if base == 0:
        return None
    return (values[-1] - base) / base


def tick_momentum(ticks: Sequence[Tick], window_seconds: float) -> float | None:
    """Signed mid-price change over the trailing window, in price units."""
    if not ticks:
        return None
    end = ticks[-1].time
    start = end - timedelta(seconds=window_seconds)
    window = [t for t in ticks if t.time >= start]
    if len(window) < 2:
        return None
    return float(window[-1].mid - window[0].mid)
