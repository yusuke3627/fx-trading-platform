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

`trading.backtest.research` rewrites every tick's known time from its broker
label before replaying, uniformly — for polled and backfilled rows alike.
Stored rows are not touched: `received_at` in the database keeps meaning
"when this row was ingested" (and stays usable for measuring the real broker
offset from polled data); the reconstruction is a replay-input concern
applied at the read boundary.

The mapping anchors on the MT5 New York-close convention: the server's wall
clock is New York's plus a fixed number of hours
(`market.broker_server_ahead_of_ny_hours`, base.yaml: 7) year-round, which
puts it at UTC+3 during US DST and UTC+2 outside it. Subtracting the anchor
and localizing the result in America/New_York therefore follows the DST
switches without a season table. A fixed UTC offset was rejected: it would
shift every winter tick an hour against the real-UTC `known_at` axis the
features and event-risk windows live on. The anchor itself is an assumption
verified only against summer data so far; the measured `received_at` of
polled winter rows will confirm or correct it, and correcting it changes no
stored data.

The repeated fall-back hour maps to its first occurrence (`fold=0`). That
one broker-labelled hour a year is genuinely ambiguous in the recorded
series itself — no rule recovers which pass a label belongs to.

Applying the reconstruction to polled rows too, where a measured value
exists, is deliberate: one rule for the whole series keeps a period that
mixes both collection paths on a single monotonic axis, and the difference
for a polled tick is the network delay — noise at the granularity anything
reading the bridge acts on.

## Consequences

- **Roles.** Reconstructed history serves hypothesis search and coarse
  validation. Final validation runs on forward-collected data, where the
  known-time axis is measured, not assumed. A strategy promoted toward live
  gets its last word from the forward-collected series.
- **The bridge is one-directional.** Bars, indicators and everything else on
  the broker axis (ADR-005) are untouched — they fold `event_time` directly
  and are exact regardless of the offset. Only known-time visibility
  (feature refreshes, event ordering) rides the reconstruction.
- **The anchor can be corrected** without touching stored data, since
  nothing is persisted with reconstructed values — a measured winter offset
  that contradicts the New York-close assumption only changes the config
  value and the mapping function.
