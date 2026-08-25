-- 0006_decision_trail_scope.sql
-- The decision trail belongs to one broker account.
-- An intent's size comes from that account's equity and a decision is graded
-- against its loss history, so both mean something different depending on
-- which account they were made for. Sharing one database across a demo and a
-- live login mixes the two, and afterwards nothing tells them apart.
-- account_snapshots was scoped the same way in 0005; there the consequence was
-- a wrong calculation, here it is a record that cannot be read back.

BEGIN;

-- Rows written before this migration carry no account, and nothing in them
-- can supply one: the runner knew which account it was evaluating, the row
-- does not. Putting a guessed account on a record whose entire point is to say
-- which account a judgement was for would be worse than not having the record,
-- so the earlier trail is removed rather than backfilled.
--
-- What that costs is the decisions recorded between the shadow runner gaining
-- its store and this migration. Shadow evaluates every few seconds and nothing
-- reads these rows yet, so recording resumes at once. Deletion follows the
-- foreign keys in reverse.
--
-- execution_commands also references position_intents, and is deliberately not
-- touched: no row there can point at one of these intents. An order is built
-- by the OMS, the shadow runner holds none, and nothing in the codebase writes
-- execution_commands yet. Should that change, this DELETE fails on the foreign
-- key — which is the right outcome, because an intent an order was built from
-- is not something to discard without a person deciding to.
DELETE FROM risk_decisions;
DELETE FROM position_intents;
DELETE FROM strategy_signals;

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
