# ADR-006: 4h and 1d bars need no session anchor

**Status:** Accepted (2026-08-25)

## Decision

`BarBuilder` folds every timeframe in `TIMEFRAME_SECONDS`, 4h and 1d included,
on the same epoch floor of `tick.time`. No trade-server session anchor is
configured, stored or measured anywhere.

The refusal added in PR #10 — a `ValueError` for any timeframe not dividing one
hour, plus the `is_foldable` / `foldable_timeframes` split that routed around
it — is removed. This closes issue #11, whose four open questions (where the
anchor comes from, how to represent it, where to put it, how to wire it) all
dissolve rather than get answered.

## Rationale

The refusal rested on one premise: that our buckets sit on a real-UTC grid
while the server's 4h and 1d candles hang off its own midnight, so the two
would disagree by the size of the server's offset. Two independent facts
established since say otherwise.

**Broker timestamps are already the server's wall clock.** ADR-005 measured it:
MT5 reports every timestamp in the server's own zone and labels it UTC, and
OANDA Japan runs three hours ahead. `_bucket_start` floors `tick.time` on the
epoch grid, so what it floors is the server's wall clock and what it produces
is the server's own grid — a 1d bucket at the server's midnight, a 4h bucket at
its 00:00 / 04:00 / 08:00. There was never a shift to correct, only a
misreading of which clock the timestamps were on.

**Nothing in this system calls `copy_rates`.** Live bars are folded by
`bar_service` from the stored tick series, with the same `BarBuilder` replay
uses. Live and replay candles agree because they are the same computation over
the same rows, not because both match an external grid. The comment claiming
"Live MT5 serves bars directly" described an intention that was never built.

**DST never enters.** Broker timestamps are never converted into our time
(ADR-005), so whether the server is a fixed UTC+3 or EET/EEST is not something
bucketing has to know — the wall clock is taken as given. A zone change is
visible in exactly one place: the autumn transition repeats an hour of server
wall clock, which would fold two candles' quotes into one bucket. That hour
falls inside the weekend closure, so no quotes straddle it, and it is left
unhandled rather than guarded against.

Equality with what the terminal draws follows from the timestamp convention
measured in ADR-005; it has not been confirmed by comparing a folded candle
against an MT5 chart side by side. That check is worth doing once on the
trading host, but no behaviour here depends on its outcome.

## Consequences

- `monetary_policy_convergence` (trigger 4h / trend 1d in `config/base.yaml`)
  now receives bars. It was previously configured for a series nobody built,
  and would have waited for it indefinitely.
- A long candle still closes only when a quote timestamped at or past its end
  arrives — the no-flush rule every timeframe follows. Friday's daily bar is
  therefore published at the Monday open, not at Saturday 00:00.
- `BarService.build_once` reaches back `COLD_START_LOOKBACK` (7 days) on a cold
  start and drops the bucket it entered halfway, so a first pass yields a
  handful of daily bars rather than a full history.
- A pass rebuilds from the last stored bar, which at 1m is a minute of quotes
  and at 1d is the trading day so far. Long timeframes are therefore polled
  behind a check that costs two index seeks and stops there while the bucket is
  still open, rather than at a slower interval — a daily bar has to appear when
  it closes, not up to a day later.
