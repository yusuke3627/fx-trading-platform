# ADR-007: Research replays reconstruct a tick's known time from its broker timestamp

**Status:** Accepted (2026-08-26)

## Context

The replay clock runs on the known-time axis: events at `known_at`, ticks at
`received_at` (a late-arriving quote was not usable at its broker timestamp).
For ticks the polling collector stores, `received_at` is the true arrival
instant and the axis is measured.

For a backfilled archive it is not. `copy_ticks_range` hands back a whole
window at once, and every tick in it gets the backfill run's wall clock as
`received_at` — a value days or years in the tick's own future, shared by the
entire window. A replay ordered on stored `received_at` would deliver an
archived day at a single instant, after every macro row in the store had
already become visible: total look-ahead, no intra-day structure.

## Decision

`trading.backtest.research` rewrites every tick's known time to
`event_time - market.broker_utc_offset_hours` before replaying, uniformly —
for polled and backfilled rows alike. Stored rows are not touched:
`received_at` in the database keeps meaning "when this row was ingested"
(and stays usable for measuring the real broker offset from polled data);
the reconstruction is a replay-input concern applied at the read boundary.

The offset is configuration (base.yaml: 3), not measurement. OANDA's MT5
server follows the US DST calendar, so a fixed value is wrong by an hour for
part of the year. The features that cross this axis bridge — macro, policy,
intervention — move at daily granularity, and one hour of skew does not
change which day's rows a replay evaluation can see, except within an hour
of the arrival instant itself.

Applying the reconstruction to polled rows too, where a measured value
exists, is deliberate: one rule for the whole series keeps a period that
mixes both collection paths on a single monotonic axis, and the difference
for a polled tick is the network delay plus the offset error — noise at the
granularity anything reading the bridge acts on.

## Consequences

- **Roles.** Reconstructed history serves hypothesis search and coarse
  validation. Final validation runs on forward-collected data, where the
  known-time axis is measured, not assumed. A strategy promoted toward live
  gets its last word from the forward-collected series.
- **The bridge is one-directional.** Bars, indicators and everything else on
  the broker axis (ADR-005) are untouched — they fold `event_time` directly
  and are exact regardless of the offset. Only known-time visibility
  (feature refreshes, event ordering) rides the reconstruction.
- **A DST-aware offset can replace the constant** without touching stored
  data, since nothing is persisted with reconstructed values.
