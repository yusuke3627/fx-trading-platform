# ADR-001: Account mode is machine-verified at startup

**Status:** Superseded by SYSTEM_SPEC v2.0 [§8.1](../SYSTEM_SPEC.md#s8-1)

本改訂の main への取り込み時に規範を移管する。以下は旧決定の履歴であり、後続決定と現存制約は移管先を参照する。
旧状態: Accepted (v1.3 frozen decision)

## Decision

At startup the platform reads `ACCOUNT_MARGIN_MODE` from MT5 and maps it to
NETTING / EXCHANGE / HEDGING. The expected mode lives in configuration
(`broker.expected_account_mode`). If actual != expected, the platform enters
`EXECUTION_DISABLED`.

## Rationale

Netting and hedging accounts have incompatible position semantics (one net
position per symbol vs multiple tickets). Trading with the wrong assumption
turns "close a short" into "open a long". Human memory is not a control.

## Consequences

- Preflight step `account_margin_mode` fails and disables execution on mismatch.
- OMS selects netting-delta vs ticket-referenced command paths by verified mode.
