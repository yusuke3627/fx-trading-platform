-- 0003_economic_releases.sql
-- Economic release PIT (docs/research/2026-08-15-pit-macro-policy-intervention.md
-- Phase A): forward collection from BLS/BEA/Census plus ALFRED vintage
-- reconstruction land in macro_observations, one row per vintage.

BEGIN;

ALTER TABLE macro_observations
    ADD COLUMN retrieved_at TIMESTAMPTZ,
    ADD COLUMN payload_hash TEXT,
    ADD COLUMN unit TEXT;

-- One row per vintage: a later value for the same (series, period) is a new
-- row with its own known_at, never an update. Re-ingesting an already-stored
-- vintage is a no-op.
ALTER TABLE macro_observations
    ADD CONSTRAINT macro_observations_vintage_key
    UNIQUE (series, observation_period, known_at);

COMMIT;
