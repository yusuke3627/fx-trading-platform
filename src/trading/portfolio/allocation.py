"""Deterministic fill attribution for intentionally batched commands.

Allocation rules are fixed before the trade and never changed after the fact:
a fill is never re-attributed to whichever strategy happened to win.
Un-allocated remainders stay as pending virtual deltas.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AllocationRequest:
    strategy_id: str
    quantity: Decimal


@dataclass(frozen=True)
class AllocationResult:
    filled: dict[str, Decimal]
    pending: dict[str, Decimal]


def allocate_pro_rata(
    requests: list[AllocationRequest],
    filled_quantity: Decimal,
    volume_step: Decimal = Decimal("1"),
) -> AllocationResult:
    """Pro-rata allocation quantized to volume_step, deterministic by
    largest-remainder then strategy_id ordering."""
    if filled_quantity < 0:
        raise ValueError("filled_quantity must be >= 0")
    total = sum((r.quantity for r in requests), Decimal("0"))
    if total <= 0:
        raise ValueError("total requested quantity must be > 0")
    if filled_quantity > total:
        raise ValueError("filled quantity exceeds requested total")

    ordered = sorted(requests, key=lambda r: r.strategy_id)

    raw = {r.strategy_id: filled_quantity * r.quantity / total for r in ordered}
    floored = {
        sid: (value // volume_step) * volume_step for sid, value in raw.items()
    }
    remainder_steps = int(
        (filled_quantity - sum(floored.values(), Decimal("0"))) / volume_step
    )

    by_fraction = sorted(
        ordered,
        key=lambda r: (-(raw[r.strategy_id] - floored[r.strategy_id]), r.strategy_id),
    )
    allocated = dict(floored)
    for r in by_fraction[:remainder_steps]:
        allocated[r.strategy_id] += volume_step

    pending = {
        r.strategy_id: r.quantity - allocated[r.strategy_id]
        for r in ordered
        if r.quantity - allocated[r.strategy_id] > 0
    }
    return AllocationResult(filled=allocated, pending=pending)
