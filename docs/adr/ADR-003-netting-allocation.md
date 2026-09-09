# ADR-003: Netting conversion and deterministic fill attribution

**Status:** Superseded by SYSTEM_SPEC v2.0 [§7.1](../SYSTEM_SPEC.md#s7-1)

本改訂の main への取り込み時に規範を移管する。以下は旧決定の履歴であり、後続決定と現存制約は移管先を参照する。
旧状態: Accepted (v1.3 frozen decision)

## Decision

Portfolio Manager aggregates per-strategy virtual targets into a desired
broker net exposure. OMS orders only the delta between desired and current
broker exposure. When multiple strategies are intentionally batched into one
command, the resulting fill is attributed pro-rata (largest-remainder,
strategy_id-ordered, volume-step-quantized); remainders stay as pending
virtual deltas. Allocation rules are fixed before the trade.

## Rationale

Netting accounts hold one net position per symbol, so raw strategy quantities
cannot be sent as orders. Attribution must be deterministic or strategy
evaluation becomes retrofit fiction.

## Consequences

- `portfolio/allocation.py` implements the deterministic allocator.
- 1 command = 1 primary strategy delta is the default; batching is explicit.
- Fill attribution is never revised after the fact.
