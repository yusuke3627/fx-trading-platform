-- 0006_decision_trail_scope.sql
-- The decision trail belongs to one broker account.
-- An intent's size comes from that account's equity and a decision is graded
-- against its loss history, so both mean something different depending on
-- which account they were made for. Sharing one database across a demo and a
-- live login mixes the two, and afterwards nothing tells them apart.
-- account_snapshots was scoped the same way in 0005; there the consequence was
-- a wrong calculation, here it is a record that cannot be read back.
--
-- The columns are NOT NULL without a default: nothing wrote these tables
-- before the shadow runner started recording, and it has not yet produced a
-- signal, so there is no row to backfill.

BEGIN;

ALTER TABLE strategy_signals ADD COLUMN account_id TEXT NOT NULL;
ALTER TABLE position_intents ADD COLUMN account_id TEXT NOT NULL;
ALTER TABLE risk_decisions   ADD COLUMN account_id TEXT NOT NULL;

-- Reads are scoped by account before anything else, so the account leads.
DROP INDEX idx_signals_strategy;
CREATE INDEX idx_signals_account_strategy
    ON strategy_signals (account_id, strategy_id, generated_at);

-- Reading back a run's decisions is "the newest for this account", which the
-- trail's join drives from risk_decisions.
CREATE INDEX idx_decisions_account_decided
    ON risk_decisions (account_id, decided_at DESC);

COMMIT;
