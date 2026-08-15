"""Realized volatility."""
from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise


def log_returns(values: Sequence[float]) -> list[float]:
    return [
        math.log(b / a)
        for a, b in pairwise(values)
        if a > 0 and b > 0
    ]


def realized_volatility(closes: Sequence[float], window: int) -> float | None:
    """Standard deviation of log returns over the trailing window."""
    if window <= 1:
        raise ValueError("window must be > 1")
    rets = log_returns(closes)
    if len(rets) < window:
        return None
    tail = rets[-window:]
    mean = sum(tail) / len(tail)
    var = sum((r - mean) ** 2 for r in tail) / len(tail)
    return math.sqrt(var)
