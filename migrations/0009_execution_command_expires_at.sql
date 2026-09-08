-- 0009_execution_command_expires_at.sql
-- Queue の再 claim 後も signal の失効時刻を復元できるようにする。

BEGIN;

ALTER TABLE execution_commands
ADD COLUMN expires_at TIMESTAMPTZ;

COMMIT;
