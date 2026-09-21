-- 状態遷移の時刻は注入 Clock の値。DB 保存時刻から遡って補完しない。
BEGIN;

ALTER TABLE execution_commands
    ADD COLUMN state_revision BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN state_changed_at TIMESTAMPTZ;

CREATE TABLE execution_state_observations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    command_id UUID NOT NULL REFERENCES execution_commands(id) ON DELETE CASCADE,
    state_revision BIGINT NOT NULL,
    state TEXT NOT NULL,
    changed_at TIMESTAMPTZ,
    quantity NUMERIC NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (command_id, state_revision)
);

-- 旧行は現在状態しか持たない。履歴行は backfill しない。
CREATE INDEX idx_commands_created_at ON execution_commands(created_at);
CREATE INDEX idx_ticks_received_at ON market_ticks(received_at, symbol);

COMMIT;
