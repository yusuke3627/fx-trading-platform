# ADR-002: Broker-side SL/TP fills are first-class (PROTECTION_FILL)

**Status:** Superseded by SYSTEM_SPEC v2.0 [§8.2](../SYSTEM_SPEC.md#s8-2)

本改訂の main への取り込み時に規範を移管する。以下は旧決定の履歴であり、後続決定と現存制約は移管先を参照する。
旧状態: Accepted (v1.3 frozen decision)

## Decision

A deal that arrives without a matching execution command, but belongs to a
known own position and carries MT5 reason SL/TP/stop-out, is accepted as
`PROTECTION_FILL` — not treated as an untracked fill.

## Rationale

Broker-side protection fires without any command from us; that is its purpose.
MT5 deals expose `DEAL_REASON_SL` / `DEAL_REASON_TP`, so these fills are
classifiable, auditable and expected.

## Consequences

- KPI `untracked_fill = 0` counts command-origin + protection-origin as tracked.
- Exit flow must check for a protection fill before treating a missing
  position as an error (protection/system-exit race).
- Micro-live onwards, every new position must have broker-side SL
  (`OPEN_UNPROTECTED` is CRITICAL).
