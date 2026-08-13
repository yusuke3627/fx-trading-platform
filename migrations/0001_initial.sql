-- 0001_initial.sql
-- Core schema for the FX trading platform (SYSTEM_SPEC v1.3).
-- Quantities are base-currency units; prices are quote-currency numerics.
-- All timestamps are timestamptz (UTC).

BEGIN;

-- ---------------------------------------------------------------------------
-- Point-in-time event store
-- ---------------------------------------------------------------------------

CREATE TABLE events (
    id              UUID PRIMARY KEY,
    event_type      TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_uri      TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',
    payload_hash    TEXT,
    raw_uri         TEXT,
    effective_at    TIMESTAMPTZ,
    published_at    TIMESTAMPTZ,
    retrieved_at    TIMESTAMPTZ NOT NULL,
    known_at        TIMESTAMPTZ NOT NULL,
    processed_at    TIMESTAMPTZ,
    superseded_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_events_known_at ON events (known_at);
CREATE INDEX idx_events_type_known_at ON events (event_type, known_at);
CREATE INDEX idx_events_source ON events (source);

-- Source registry is a logical view over events, never a second store.
CREATE VIEW source_registry AS
SELECT
    source,
    source_uri,
    retrieved_at,
    published_at,
    known_at,
    payload_hash,
    raw_uri
FROM events;

CREATE TABLE macro_observations (
    id                  UUID PRIMARY KEY,
    series              TEXT NOT NULL,
    observation_period  TEXT NOT NULL,
    value               NUMERIC,
    revision_of         UUID REFERENCES macro_observations (id),
    source              TEXT NOT NULL,
    source_uri          TEXT,
    effective_at        TIMESTAMPTZ,
    published_at        TIMESTAMPTZ,
    known_at            TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_macro_observations_series ON macro_observations (series, known_at);

CREATE TABLE fundamental_events (
    id          UUID PRIMARY KEY,
    event_id    UUID REFERENCES events (id),
    category    TEXT NOT NULL,
    direction   TEXT,
    magnitude   NUMERIC,
    details     JSONB NOT NULL DEFAULT '{}',
    known_at    TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Verification-status transitions (e.g. intervention RUMOR ->
-- OFFICIAL_AMOUNT_CONFIRMED) are appended, never updated in place.
CREATE TABLE verification_events (
    id                UUID PRIMARY KEY,
    subject_event_id  UUID NOT NULL,
    from_status       TEXT,
    to_status         TEXT NOT NULL,
    known_at          TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_verification_subject ON verification_events (subject_event_id, known_at);

-- ---------------------------------------------------------------------------
-- Market data
-- ---------------------------------------------------------------------------

CREATE TABLE market_ticks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol       TEXT NOT NULL,
    bid          NUMERIC NOT NULL,
    ask          NUMERIC NOT NULL,
    tick_time    TIMESTAMPTZ NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_market_ticks_symbol_time ON market_ticks (symbol, tick_time);

CREATE TABLE market_bars (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol       TEXT NOT NULL,
    timeframe    TEXT NOT NULL,
    start_time   TIMESTAMPTZ NOT NULL,
    open         NUMERIC NOT NULL,
    high         NUMERIC NOT NULL,
    low          NUMERIC NOT NULL,
    close        NUMERIC NOT NULL,
    tick_volume  BIGINT NOT NULL DEFAULT 0,
    UNIQUE (symbol, timeframe, start_time)
);

-- ---------------------------------------------------------------------------
-- Strategy output
-- ---------------------------------------------------------------------------

CREATE TABLE strategy_signals (
    id                       UUID PRIMARY KEY,
    strategy_id              TEXT NOT NULL,
    strategy_version         TEXT NOT NULL,
    symbol                   TEXT NOT NULL,
    desired_direction        TEXT NOT NULL CHECK (desired_direction IN ('LONG', 'SHORT')),
    conviction               DOUBLE PRECISION NOT NULL,
    expected_horizon_seconds INTEGER NOT NULL,
    stop_distance_pips       NUMERIC NOT NULL,
    reason_codes             JSONB NOT NULL DEFAULT '[]',
    generated_at             TIMESTAMPTZ NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_signals_strategy ON strategy_signals (strategy_id, generated_at);

CREATE TABLE position_intents (
    id                           UUID PRIMARY KEY,
    signal_id                    UUID REFERENCES strategy_signals (id),
    strategy_id                  TEXT NOT NULL,
    strategy_version             TEXT NOT NULL,
    symbol                       TEXT NOT NULL,
    action                       TEXT NOT NULL
        CHECK (action IN ('OPEN', 'INCREASE', 'REDUCE', 'CLOSE')),
    direction                    TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    target_quantity              NUMERIC,
    delta_quantity               NUMERIC,
    stop_loss_price              NUMERIC,
    take_profit_price            NUMERIC,
    protection_source            TEXT
        CHECK (protection_source IN ('STRATEGY', 'RISK_OVERRIDE', 'EMERGENCY')),
    maximum_unprotected_seconds  INTEGER,
    reason_codes                 JSONB NOT NULL DEFAULT '[]',
    generated_at                 TIMESTAMPTZ NOT NULL,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Snapshot history: the current position is MAX(as_of) per
-- (strategy_id, symbol). Rows are never updated.
CREATE TABLE virtual_positions (
    id             UUID PRIMARY KEY,
    strategy_id    TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    direction      TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    quantity       NUMERIC NOT NULL CHECK (quantity >= 0),
    average_price  NUMERIC,
    as_of          TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_virtual_positions_current
    ON virtual_positions (strategy_id, symbol, as_of DESC);

-- ---------------------------------------------------------------------------
-- Risk
-- ---------------------------------------------------------------------------

CREATE TABLE risk_decisions (
    id                 UUID PRIMARY KEY,
    intent_id          UUID REFERENCES position_intents (id),
    approved           BOOLEAN NOT NULL,
    approved_quantity  NUMERIC,
    checks             JSONB NOT NULL DEFAULT '[]',
    reject_codes       JSONB NOT NULL DEFAULT '[]',
    decided_at         TIMESTAMPTZ NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE account_snapshots (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observed_at        TIMESTAMPTZ NOT NULL,
    balance            NUMERIC NOT NULL,
    equity             NUMERIC NOT NULL,
    margin             NUMERIC NOT NULL,
    free_margin        NUMERIC NOT NULL,
    margin_level       NUMERIC,
    unrealized_pnl     NUMERIC NOT NULL,
    realized_pnl_day   NUMERIC NOT NULL,
    high_water_mark    NUMERIC NOT NULL,
    drawdown_from_hwm  NUMERIC NOT NULL,
    broker_connected   BOOLEAN NOT NULL
);

CREATE INDEX idx_account_snapshots_observed ON account_snapshots (observed_at);

-- ---------------------------------------------------------------------------
-- Execution
-- ---------------------------------------------------------------------------

CREATE TABLE execution_commands (
    id                         UUID PRIMARY KEY,
    intent_id                  UUID REFERENCES position_intents (id),
    idempotency_key            TEXT NOT NULL UNIQUE,
    symbol                     TEXT NOT NULL,
    side                       TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    action                     TEXT NOT NULL
        CHECK (action IN ('OPEN', 'INCREASE', 'REDUCE', 'CLOSE')),
    direction                  TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    quantity                   NUMERIC NOT NULL CHECK (quantity > 0),
    stop_loss_price            NUMERIC,
    take_profit_price          NUMERIC,
    broker_position_ticket     TEXT,
    state                      TEXT NOT NULL DEFAULT 'CREATED'
        CHECK (state IN (
            'CREATED', 'RISK_APPROVED', 'READY', 'CLAIMED', 'SUBMITTING',
            'ACKNOWLEDGED', 'PARTIAL_FILL', 'FILLED',
            'REJECTED', 'CANCELLED', 'EXPIRED', 'UNKNOWN'
        )),
    claimed_by                 TEXT,
    claimed_at                 TIMESTAMPTZ,
    claim_expires_at           TIMESTAMPTZ,
    submitting_at              TIMESTAMPTZ,
    broker_request_started_at  TIMESTAMPTZ,
    broker_retcode             INTEGER,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Supports the FOR UPDATE SKIP LOCKED claim query.
CREATE INDEX idx_execution_commands_claim ON execution_commands (state, created_at);

CREATE TABLE broker_orders (
    id               UUID PRIMARY KEY,
    command_id       UUID REFERENCES execution_commands (id),
    broker_order_id  TEXT NOT NULL UNIQUE,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity         NUMERIC NOT NULL,
    state            TEXT NOT NULL,
    placed_at        TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE fills (
    id                          UUID PRIMARY KEY,
    broker_deal_id              TEXT NOT NULL UNIQUE,
    broker_order_id             TEXT,
    -- Ticket AND lifecycle identifier are both mandatory to record when
    -- present: netting reversals break ticket-only tracking.
    broker_position_ticket      TEXT,
    broker_position_identifier  TEXT,
    execution_command_id        UUID REFERENCES execution_commands (id),
    origin                      TEXT NOT NULL
        CHECK (origin IN ('COMMAND', 'PROTECTION', 'BROKER_EXTERNAL')),
    protection_reason           TEXT
        CHECK (protection_reason IN ('STOP_LOSS', 'TAKE_PROFIT', 'STOP_OUT')),
    side                        TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity                    NUMERIC NOT NULL,
    price                       NUMERIC NOT NULL,
    broker_time                 TIMESTAMPTZ NOT NULL,
    received_at                 TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_fills_command ON fills (execution_command_id);
CREATE INDEX idx_fills_position ON fills (broker_position_identifier);

-- Broker position observations (snapshot history, not upserts).
CREATE TABLE broker_positions (
    id                          UUID PRIMARY KEY,
    broker_position_ticket      TEXT NOT NULL,
    broker_position_identifier  TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    direction                   TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    quantity                    NUMERIC NOT NULL,
    entry_price                 NUMERIC,
    stop_loss                   NUMERIC,
    take_profit                 NUMERIC,
    observed_at                 TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_broker_positions_ticket
    ON broker_positions (broker_position_ticket, observed_at DESC);

-- ---------------------------------------------------------------------------
-- Operations
-- ---------------------------------------------------------------------------

CREATE TABLE reconciliation_runs (
    id                UUID PRIMARY KEY,
    trigger           TEXT NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ,
    commands_checked  INTEGER NOT NULL DEFAULT 0,
    deals_checked     INTEGER NOT NULL DEFAULT 0,
    untracked_fills   INTEGER NOT NULL DEFAULT 0,
    incidents         JSONB NOT NULL DEFAULT '[]',
    healthy           BOOLEAN
);

CREATE TABLE system_incidents (
    id           UUID PRIMARY KEY,
    severity     TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL')),
    kind         TEXT NOT NULL,
    detail       JSONB NOT NULL DEFAULT '{}',
    occurred_at  TIMESTAMPTZ NOT NULL,
    resolved_at  TIMESTAMPTZ
);

CREATE TABLE config_versions (
    id            UUID PRIMARY KEY,
    environment   TEXT NOT NULL,
    version_label TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    content       JSONB NOT NULL,
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE backtest_runs (
    id            UUID PRIMARY KEY,
    strategy_id   TEXT NOT NULL,
    code_version  TEXT NOT NULL,
    config_hash   TEXT NOT NULL,
    data_from     TIMESTAMPTZ NOT NULL,
    data_to       TIMESTAMPTZ NOT NULL,
    seed          BIGINT NOT NULL,
    scenario      TEXT NOT NULL,
    metrics       JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
