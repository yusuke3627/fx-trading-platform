"""Exponential moving average."""
from __future__ import annotations

from typing import Sequence


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """EMA series seeded with the SMA of the first `period` values."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return []
    alpha = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(out[-1] + alpha * (v - out[-1]))
    return out


def ema(values: Sequence[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None
