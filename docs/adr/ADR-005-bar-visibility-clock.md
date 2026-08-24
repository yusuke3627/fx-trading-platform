# ADR-005: Bars are bucketed on the broker clock and made visible on ours

**Status:** Accepted (2026-08-24)

## Decision

A `Bar` carries two timestamps on two different clocks, and they are never
compared with each other:

- `start` (and the derived `close_time`) is the candle's span on the **broker's
  clock**. It decides which quotes belong to the bar.
- `known_at` is the **real UTC instant this system observed the bar complete**:
  the `received_at` of the tick whose broker timestamp first reached the
  bucket's end. It decides when the bar may be shown to a strategy.

`BarBuilder` therefore judges both folding and completion on `tick.time`
(broker), never on `tick.known_time`. Visibility — the replay clock, the
`InMemoryMarketData` filter, `replay_time()` — reads `known_at` only.

The `market_bars` CHECK constraint `known_at >= end_at` is dropped: it encoded
the assumption that both columns sit on one clock.

## Rationale

MT5 reports every timestamp in the broker's own zone and labels it UTC. OANDA
Japan runs UTC+3, measured on the demo account (n=262):

```sql
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY received_at - event_time)
FROM market_ticks;
-- -02:59:59.897284
```

The offset is not an occasional skew that a wider tolerance absorbs — it is
constant and three hours wide. The previous `BarBuilder` bucketed on
`tick.time` (broker) but decided completion with `tick.known_time`
(`received_at`, real UTC). Under a constant offset that comparison can never
succeed, and the builder emits nothing at all:

```
offset 0h  (broker == receive clock): ticks=30 bars=4
offset +3h (measured)               : ticks=30 bars=0
```

Two repairs were available and rejected:

- **Correcting broker timestamps at ingestion.** The offset is not exposed by
  the MT5 API, cannot be read from a stale tick while the market is closed,
  and shifts twice a year with DST. Storing a guessed correction makes every
  stored quote depend on that guess.
- **Converting `received_at` into broker space before comparing.** It keeps
  the PIT axis in the broker's zone, so economic releases — whose `known_at`
  comes from BLS/BEA/Census in real UTC — stay three hours out of step with
  market data. The problem moves rather than closes.

Splitting the clocks removes the need to know the offset at all. Bucketing
uses only broker timestamps, so candles match what the terminal draws;
visibility uses only real timestamps, so a bar and a CPI release are ordered
correctly against the same replay clock.

## Consequences

- `Bar.known_at` is required. `close_time` keeps its meaning (the bar's end on
  the broker clock) but is no longer the visibility instant.
- `replay_time(Bar)` returns `known_at`; `InMemoryMarketData` filters bars by
  `known_at`.
- `_row_to_bar` reads `known_at` back instead of re-deriving it — the two
  columns are now independent values, so one cannot stand in for the other.
- Migration `0004_bar_known_at.sql` drops `market_bars_known_at_check`.
- **Bars built from backfilled ticks carry the backfill's `received_at`.** That
  is the honest answer for point-in-time replay of this system — it did not
  hold those quotes earlier — but it means a gap repaired later stays invisible
  to replays positioned before the repair. Research over a period that was
  backfilled must account for this rather than assume the bars were live.
