-- 0002_market_data.sql
-- Point-in-time market data: tick provenance and bar visibility.
-- 0001 created market_ticks / market_bars in a minimal shape; this migration
-- brings them to the PIT contract, where a bar is known at its own close.
-- All timestamps are timestamptz (UTC); JST is a display and risk-day concern.

BEGIN;

ALTER TABLE market_ticks RENAME COLUMN tick_time TO event_time;

-- Provenance of the ingesting batch: a run that recorded bad data has to be
-- identifiable and removable after the fact.
ALTER TABLE market_ticks
    ADD COLUMN source        TEXT NOT NULL,
    ADD COLUMN ingestion_run UUID NOT NULL;

-- Re-ingesting an archive must not duplicate quotes. A repeated
-- (symbol, event_time) carrying a DIFFERENT bid/ask is a genuine second
-- quote within the same second and is kept.
ALTER TABLE market_ticks
    ADD CONSTRAINT market_ticks_quote_key UNIQUE (symbol, event_time, bid, ask);

-- The constraint's index leads with (symbol, event_time) and serves every
-- lookup the dropped one did. Ticks are the highest-volume inserts in the
-- system, so the table must not carry two indexes on the same leading columns.
DROP INDEX idx_market_ticks_symbol_time;

ALTER TABLE market_bars RENAME COLUMN start_time TO start_at;
ALTER TABLE market_bars
    RENAME CONSTRAINT market_bars_symbol_timeframe_start_time_key
    TO market_bars_symbol_timeframe_start_at_key;

-- end_at and known_at are written from the domain's Bar.close_time
-- (start + TIMEFRAME_SECONDS[timeframe]), so the timeframe table is not
-- duplicated here. The checks only reject rows that contradict it.
ALTER TABLE market_bars
    ADD COLUMN end_at   TIMESTAMPTZ NOT NULL,
    ADD COLUMN known_at TIMESTAMPTZ NOT NULL,
    ADD CONSTRAINT market_bars_span_check CHECK (end_at > start_at),
    -- High, low and close exist only once the bar has closed: a bar cannot
    -- have been known before its own end.
    ADD CONSTRAINT market_bars_known_at_check CHECK (known_at >= end_at);

CREATE INDEX idx_market_bars_visibility ON market_bars (symbol, timeframe, known_at);

COMMIT;
