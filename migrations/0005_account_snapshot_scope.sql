-- 0005_account_snapshot_scope.sql
-- Account snapshots belong to one broker account.
-- The high-water mark and the JST day baseline are carried forward from the
-- stored series, so a terminal moved from the demo login to a live one against
-- this database would inherit the other account's peak equity and day opening.
-- The equities are unrelated, and the drawdown computed across them is not a
-- drawdown at all.
--
-- The column is NOT NULL without a default: nothing wrote this table before
-- the account collector existed, so there is no row to backfill and a row
-- appearing here would mean the assumption is wrong.

BEGIN;

ALTER TABLE account_snapshots ADD COLUMN account_id TEXT NOT NULL;

-- Every read is scoped by account before it is ordered by time, so the index
-- has to lead with the account. The replaced one served the unscoped queries
-- that no longer exist.
DROP INDEX idx_account_snapshots_observed;
CREATE INDEX idx_account_snapshots_account_observed
    ON account_snapshots (account_id, observed_at);

COMMIT;
