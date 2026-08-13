"""Market structure: rolling extremes, swing points, failed breakouts."""
from __future__ import annotations

from collections.abc import Sequence

from trading.domain.market import Bar


def rolling_high(bars: Sequence[Bar], lookback: int) -> float | None:
    if not bars:
        return None
    return max(float(b.high) for b in bars[-lookback:])


def rolling_low(bars: Sequence[Bar], lookback: int) -> float | None:
    if not bars:
        return None
    return min(float(b.low) for b in bars[-lookback:])


def swing_highs(bars: Sequence[Bar], left: int = 2, right: int = 2) -> list[int]:
    """Indices of bars whose high exceeds `left` bars before and `right` after."""
    out: list[int] = []
    for i in range(left, len(bars) - right):
        h = float(bars[i].high)
        before = all(h > float(bars[j].high) for j in range(i - left, i))
        after = all(h > float(bars[j].high) for j in range(i + 1, i + right + 1))
        if before and after:
            out.append(i)
    return out


def swing_lows(bars: Sequence[Bar], left: int = 2, right: int = 2) -> list[int]:
    out: list[int] = []
    for i in range(left, len(bars) - right):
        lo = float(bars[i].low)
        before = all(lo < float(bars[j].low) for j in range(i - left, i))
        after = all(lo < float(bars[j].low) for j in range(i + 1, i + right + 1))
        if before and after:
            out.append(i)
    return out


def is_lower_high(bars: Sequence[Bar], left: int = 2, right: int = 2) -> bool:
    """True when the two most recent swing highs are descending."""
    idx = swing_highs(bars, left, right)
    if len(idx) < 2:
        return False
    return float(bars[idx[-1]].high) < float(bars[idx[-2]].high)


def detect_failed_breakout(bars: Sequence[Bar], level: float, side: str = "UP") -> bool:
    """Failed breakout on the last two closed bars.

    UP: the prior bar traded above `level` but closed back below it, and the
    last bar failed to reclaim the level. DOWN is the mirror image.
    """
    if len(bars) < 2:
        return False
    attempt, confirm = bars[-2], bars[-1]
    if side == "UP":
        attempted = float(attempt.high) > level and float(attempt.close) < level
        failed_recovery = float(confirm.close) < level
        return attempted and failed_recovery
    if side == "DOWN":
        attempted = float(attempt.low) < level and float(attempt.close) > level
        failed_recovery = float(confirm.close) > level
        return attempted and failed_recovery
    raise ValueError(f"side must be UP or DOWN, got {side!r}")
