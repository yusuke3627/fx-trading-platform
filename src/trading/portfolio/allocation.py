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
    volume_step: Decimal = Decimal(1),
) -> AllocationResult:
    """Pro-rata allocation quantized to volume_step, deterministic by
    largest-remainder then strategy_id ordering.

    Requests sharing a strategy_id are aggregated first — a dict keyed by id
    would otherwise let a later request overwrite an earlier one while the
    total still counted both, silently losing filled quantity.
    """
    if filled_quantity < 0:
        raise ValueError("filled_quantity must be >= 0")
    if filled_quantity % volume_step != 0:
        # Brokers fill in step multiples; silently flooring a misaligned fill
        # here would drop quantity from the ledger.
        raise ValueError("filled_quantity must be a multiple of volume_step")

    requested: dict[str, Decimal] = {}
    for r in requests:
        requested[r.strategy_id] = requested.get(r.strategy_id, Decimal(0)) + r.quantity

    total = sum(requested.values(), Decimal(0))
    if total <= 0:
        raise ValueError("total requested quantity must be > 0")
    if filled_quantity > total:
        raise ValueError("filled quantity exceeds requested total")

    ordered_ids = sorted(requested)

    raw = {sid: filled_quantity * requested[sid] / total for sid in ordered_ids}
    floored = {sid: (value // volume_step) * volume_step for sid, value in raw.items()}
    remainder_steps = int(
        (filled_quantity - sum(floored.values(), Decimal(0))) / volume_step
    )

    by_fraction = sorted(ordered_ids, key=lambda sid: (-(raw[sid] - floored[sid]), sid))
    allocated = dict(floored)
    for sid in by_fraction[:remainder_steps]:
        allocated[sid] += volume_step

    pending = {
        sid: requested[sid] - allocated[sid]
        for sid in ordered_ids
        if requested[sid] - allocated[sid] > 0
    }
    return AllocationResult(filled=allocated, pending=pending)
