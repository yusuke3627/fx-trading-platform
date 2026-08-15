# ADR-004: Risk day is measured three ways

**Status:** Accepted (v1.3 frozen decision)

## Decision

Loss limits are evaluated on three complementary windows, all sourced from
persisted `account_snapshots`:

1. JST calendar day (00:00–23:59:59 JST)
2. Rolling 24 hours
3. High-water-mark drawdown

## Rationale

A calendar-day limit alone resets at midnight: −0.70% at 23:55 plus −0.70% at
00:05 is −1.4% in ten minutes yet passes a daily check. Rolling 24h closes
that hole; HWM drawdown catches slow bleeds across days.

## Consequences

- `risk/limits.py` implements all three from snapshots.
- Config keys: `daily_loss_halt_pct`, `rolling_24h_loss_halt_pct`,
  `high_water_mark_drawdown_halt_pct`.
