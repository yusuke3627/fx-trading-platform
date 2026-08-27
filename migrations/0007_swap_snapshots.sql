-- 0007_swap_snapshots.sql
-- Swap/rollover の PIT broker cost data（ADR-016）。MT5 symbol properties の
-- swap 部分を forward snapshot として保存する。1 行 = 1 観測（known_at =
-- 取得時刻）。per-day 倍率は terminal のビルドによって公開されないため NULL 可。

BEGIN;

CREATE TABLE swap_snapshots (
    id                  UUID PRIMARY KEY,
    symbol              TEXT NOT NULL,
    swap_mode           INTEGER NOT NULL,
    swap_long           NUMERIC NOT NULL,
    swap_short          NUMERIC NOT NULL,
    -- 3日分 swap を課す曜日（MQL5 ENUM_DAY_OF_WEEK: 0=Sunday .. 6=Saturday）
    swap_rollover3days  INTEGER NOT NULL,
    swap_sunday         INTEGER,
    swap_monday         INTEGER,
    swap_tuesday        INTEGER,
    swap_wednesday      INTEGER,
    swap_thursday       INTEGER,
    swap_friday         INTEGER,
    swap_saturday       INTEGER,
    payload_hash        TEXT,
    retrieved_at        TIMESTAMPTZ NOT NULL,
    known_at            TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_swap_snapshots_symbol ON swap_snapshots (symbol, known_at);

COMMIT;
