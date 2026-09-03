-- 0008_arbitration_decisions.sql
-- Portfolio Arbitrator の裁定記録（ADR-029）。sized entry intent 1 件につき 1 行で、
-- 受理・却下の別と reason code、priority 順位を残す。却下された候補は Risk に届かない
-- ため risk_decisions 行を持たず、この表だけが「なぜ grade されなかったか」を語る。
-- strategy_signals.expected_edge_r は候補の期待 edge（R 倍数）。strategy が推定を
-- 持つまでは 1R = 中立で、既存行もその意味で backfill される。

BEGIN;

ALTER TABLE strategy_signals ADD COLUMN expected_edge_r NUMERIC NOT NULL DEFAULT 1;

CREATE TABLE arbitration_decisions (
    id           UUID PRIMARY KEY,
    account_id   TEXT NOT NULL,
    intent_id    UUID NOT NULL UNIQUE REFERENCES position_intents (id),
    accepted     BOOLEAN NOT NULL,
    reason_code  TEXT NOT NULL,
    -- validity（expiry / trading 不可）で落ちた候補は順位を持たない。
    rank         INTEGER,
    priority     NUMERIC,
    detail       TEXT,
    decided_at   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_arbitration_account_decided
    ON arbitration_decisions (account_id, decided_at DESC);

COMMIT;
