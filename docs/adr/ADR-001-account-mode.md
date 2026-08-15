# ADR-001: Account mode is machine-verified at startup

**Status:** Accepted (v1.3 frozen decision)

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
