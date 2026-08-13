"""Volume-weighted average price over bars.

FX tick volume is used as weight; bars with zero volume weight equally so a
session VWAP is still defined on sparse data.
"""
from __future__ import annotations

from collections.abc import Sequence

from trading.domain.market import Bar


def typical_price(bar: Bar) -> float:
    return (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0


def vwap(bars: Sequence[Bar]) -> float | None:
    if not bars:
        return None
    if all(b.tick_volume == 0 for b in bars):
        return sum(typical_price(b) for b in bars) / len(bars)
    num = sum(typical_price(b) * b.tick_volume for b in bars)
    den = sum(b.tick_volume for b in bars)
    return num / den
