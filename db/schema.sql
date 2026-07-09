-- =====================================================================
-- Stock Investment Analysis App — Data Contract v1.0 (DDL)
-- Source: system design deck §20 (Data Model). Target: PostgreSQL 16 + TimescaleDB.
-- Owner: A3 Solution Architect. Frozen as G1 baseline; changes via RTM change control.
--
-- Conventions:
--   * Money / prices / ratios: NUMERIC (never float) — decimal-safe (A5 commitment).
--   * Signal scores: NUMERIC(4,2) in range [-2.00, +2.00].
--   * All fact tables FK -> ticker(id) ON DELETE CASCADE.
--   * "* = Timescale hypertable" tables are partitioned by their date/time column.
--   * Sensitive columns (NFR-05) stored as BYTEA via pgcrypto pgp_sym_encrypt.
--     CURRENT (localhost testing): symmetric key from APP_ENCRYPTION_KEY env via the
--     KeyProvider abstraction (ADR-004) — never stored in DB or repo.
--     FUTURE (cloud prod, deferred): key from Azure Key Vault + managed identity + DB TDE.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------
-- MARKET FACTS
-- ---------------------------------------------------------------------

-- Hub entity: one row per covered instrument.
CREATE TABLE ticker (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol        TEXT        NOT NULL,                    -- e.g. '2330', 'AAPL'
    exchange      TEXT        NOT NULL,                    -- 'TWSE' | 'TPEx' | 'US'
    full_symbol   TEXT        NOT NULL UNIQUE,             -- e.g. '2330.TW', '6488.TWO', 'AAPL'
    name          TEXT,
    sector        TEXT,                                    -- used by SECTOR_NOT_COVERED gate
    is_covered    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_ticker_symbol ON ticker (symbol);

-- price_bar * (hypertable) — daily OHLCV.
CREATE TABLE price_bar (
    ticker_id  BIGINT       NOT NULL REFERENCES ticker(id) ON DELETE CASCADE,
    bar_date   DATE         NOT NULL,
    open       NUMERIC(18,4),
    high       NUMERIC(18,4),
    low        NUMERIC(18,4),
    close      NUMERIC(18,4),
    volume     BIGINT,
    source     TEXT         NOT NULL DEFAULT 'yfinance',
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),   -- row-level provenance; set to now() on upsert (v1.2.5)
    PRIMARY KEY (ticker_id, bar_date)
);
SELECT create_hypertable('price_bar', 'bar_date', chunk_time_interval => INTERVAL '1 year');

-- technical_indicator * (hypertable) — derived per-day indicators + module score.
CREATE TABLE technical_indicator (
    ticker_id    BIGINT      NOT NULL REFERENCES ticker(id) ON DELETE CASCADE,
    calc_date    DATE        NOT NULL,
    ma20         NUMERIC(18,4),
    ma60         NUMERIC(18,4),
    rsi14        NUMERIC(6,2),
    macd         NUMERIC(18,6),
    macd_signal  NUMERIC(18,6),
    macd_hist    NUMERIC(18,6),
    score        NUMERIC(4,2),        -- technical module signal, [-2,2]
    PRIMARY KEY (ticker_id, calc_date)
);
SELECT create_hypertable('technical_indicator', 'calc_date', chunk_time_interval => INTERVAL '1 year');

-- fundamental — valuation + profitability snapshot + module score. (low cardinality, plain table)
CREATE TABLE fundamental (
    ticker_id     BIGINT      NOT NULL REFERENCES ticker(id) ON DELETE CASCADE,
    asof_date     DATE        NOT NULL,
    pe            NUMERIC(12,4),
    pb            NUMERIC(12,4),
    ev_ebitda     NUMERIC(12,4),
    revenue       NUMERIC(20,2),
    eps           NUMERIC(12,4),
    gross_margin  NUMERIC(7,4),
    op_margin     NUMERIC(7,4),
    net_margin    NUMERIC(7,4),
    score         NUMERIC(4,2),       -- fundamental module signal, [-2,2]
    source        TEXT        NOT NULL DEFAULT 'yfinance',
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),   -- row-level provenance; set to now() on upsert (v1.2.5)
    PRIMARY KEY (ticker_id, asof_date)
);

-- chip_data_tw * (hypertable) — TW institutional/chip facts + module score.
CREATE TABLE chip_data_tw (
    ticker_id             BIGINT  NOT NULL REFERENCES ticker(id) ON DELETE CASCADE,
    trade_date            DATE    NOT NULL,
    foreign_net           BIGINT,          -- three-institution net components (shares or lots)
    investment_trust_net  BIGINT,
    dealer_net            BIGINT,
    margin_balance        BIGINT,
    block_trade_volume    BIGINT,
    score                 NUMERIC(4,2),    -- chip module signal (TW), [-2,2]
    source                TEXT        NOT NULL DEFAULT 'twse_tpex',
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),   -- row-level provenance (v1.2.5)
    PRIMARY KEY (ticker_id, trade_date)
);
SELECT create_hypertable('chip_data_tw', 'trade_date', chunk_time_interval => INTERVAL '1 year');

-- institutional_position_us — SEC EDGAR 13F quarterly positioning + module score.
CREATE TABLE institutional_position_us (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker_id    BIGINT      NOT NULL REFERENCES ticker(id) ON DELETE CASCADE,
    quarter      DATE        NOT NULL,          -- quarter-end date (13F is quarterly + delayed, R-04)
    filer_name   TEXT,
    shares       BIGINT,
    market_value NUMERIC(20,2),
    score        NUMERIC(4,2),                  -- chip module signal (US), [-2,2]
    source       TEXT        NOT NULL DEFAULT 'edgar_13f',
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),   -- row-level provenance (v1.2.5)
    UNIQUE (ticker_id, quarter, filer_name)
);
CREATE INDEX ix_inst_us_ticker_qtr ON institutional_position_us (ticker_id, quarter);

-- news_item — GDELT headline + VADER sentiment + module score.
CREATE TABLE news_item (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker_id     BIGINT      NOT NULL REFERENCES ticker(id) ON DELETE CASCADE,
    published_at  TIMESTAMPTZ NOT NULL,
    headline      TEXT        NOT NULL,
    url           TEXT,
    source_name   TEXT,
    sentiment     NUMERIC(5,4),                 -- VADER compound [-1,1]
    score         NUMERIC(4,2),                 -- informational module signal, [-2,2]
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),   -- row-level provenance; source_name already present (v1.2.5)
    UNIQUE (ticker_id, url, published_at)
);
CREATE INDEX ix_news_ticker_time ON news_item (ticker_id, published_at DESC);

-- ---------------------------------------------------------------------
-- DECISION & EVALUATION
-- ---------------------------------------------------------------------

-- recommendation — the immutable daily snapshot AND history log.
-- Never UPDATEd after write; annotations live in user_decision_log.
CREATE TABLE recommendation (
    ticker_id            BIGINT      NOT NULL REFERENCES ticker(id) ON DELETE CASCADE,
    rec_date             DATE        NOT NULL,
    -- NULLABLE by design: a SUPPRESSED row ("Analysis Only — Insufficient Data")
    -- carries NO score/target/confidence. Enforced by ck_rec_suppressed_shape below.
    composite_signal     NUMERIC(4,2),                      -- [-2,2] weighted composite; NULL iff SUPPRESSED
    composite_call       TEXT        NOT NULL,              -- STRONG_SELL|SELL|HOLD|BUY|STRONG_BUY | SUPPRESSED
    target_price_base    NUMERIC(18,4),
    target_price_bear    NUMERIC(18,4),
    target_price_bull    NUMERIC(18,4),
    confidence_level     TEXT,                              -- HIGH|MEDIUM|LOW
    confidence_pct       NUMERIC(5,2),                      -- module-agreement %
    conflict_flag        BOOLEAN     NOT NULL DEFAULT FALSE,-- score spread > 2.0
    horizon_months       SMALLINT    NOT NULL DEFAULT 6,    -- 3|6|12
    per_module_breakdown JSONB       NOT NULL,              -- see openapi PerModuleBreakdown; non-empty
    data_completeness    NUMERIC(4,3) NOT NULL,             -- 0.000..1.000 of 4 scoring modules
    reduced_confidence   BOOLEAN     NOT NULL DEFAULT FALSE,
    methodology_version  TEXT        NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker_id, rec_date),
    CONSTRAINT ck_rec_signal_range CHECK (composite_signal BETWEEN -2 AND 2),  -- NULL tolerated
    CONSTRAINT ck_rec_breakdown_nonempty CHECK (jsonb_array_length(per_module_breakdown) > 0),
    -- Suppressed rows MUST NOT display a score/target; scored rows MUST carry a signal.
    -- Prevents the silent-degradation failure mode (FR-35/37, A6 D2 BLOCK gate).
    CONSTRAINT ck_rec_suppressed_shape CHECK (
        (composite_call = 'SUPPRESSED'
            AND composite_signal  IS NULL
            AND target_price_base IS NULL
            AND target_price_bear IS NULL
            AND target_price_bull IS NULL
            AND confidence_level  IS NULL)
        OR
        (composite_call <> 'SUPPRESSED'
            AND composite_signal IS NOT NULL)
    )
);
CREATE INDEX ix_rec_date ON recommendation (rec_date DESC);

-- user_decision_log — what the user actually did; annotates recommendation, never mutates it.
-- transaction_price + notes are personal financial data -> encrypted at rest (NFR-05).
CREATE TABLE user_decision_log (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker_id             BIGINT      NOT NULL,
    recommendation_date   DATE        NOT NULL,
    decision              TEXT        NOT NULL,             -- 'followed' | 'ignored' | 'partial'
    transaction_price_enc BYTEA,                            -- pgp_sym_encrypt(NUMERIC::text, key)
    notes_enc             BYTEA,                            -- pgp_sym_encrypt(text, key)
    logged_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (ticker_id, recommendation_date)
        REFERENCES recommendation(ticker_id, rec_date) ON DELETE CASCADE,
    CONSTRAINT ck_decision CHECK (decision IN ('followed','ignored','partial'))
);
CREATE INDEX ix_decision_rec ON user_decision_log (ticker_id, recommendation_date);

-- backtest_result — rolling accuracy segmented by data-completeness.
CREATE TABLE backtest_result (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    as_of_date           DATE        NOT NULL,
    window_months        SMALLINT    NOT NULL,              -- 3|6|12
    completeness_segment TEXT        NOT NULL,              -- 'full' | 'partial'
    rolling_accuracy     NUMERIC(6,4),                      -- NULL if insufficient history (<12mo)
    estimated_return     NUMERIC(8,4),
    sample_size          INTEGER,
    methodology_version  TEXT        NOT NULL,
    UNIQUE (as_of_date, window_months, completeness_segment, methodology_version)
);

-- ---------------------------------------------------------------------
-- DISCOVERY, OPS & CONFIG
-- ---------------------------------------------------------------------

-- supply_chain_node / edge — silicon-wafer graph (discovery only, editable w/o deploy, R-02).
CREATE TABLE supply_chain_node (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker_id  BIGINT REFERENCES ticker(id) ON DELETE SET NULL,   -- nullable: not every node is investable
    name       TEXT   NOT NULL,
    sector     TEXT,
    role       TEXT   NOT NULL,                                    -- 'fab'|'upstream_supplier'|'downstream_customer'
    CONSTRAINT ck_node_role CHECK (role IN ('fab','upstream_supplier','downstream_customer'))
);

CREATE TABLE supply_chain_edge (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_node         BIGINT NOT NULL REFERENCES supply_chain_node(id) ON DELETE CASCADE,
    to_node           BIGINT NOT NULL REFERENCES supply_chain_node(id) ON DELETE CASCADE,
    relationship_type TEXT   NOT NULL,
    CONSTRAINT ck_edge_distinct CHECK (from_node <> to_node),
    UNIQUE (from_node, to_node, relationship_type)
);

-- pipeline_run — per-source ingestion status, drives /pipeline/status + R-01 alerting.
-- run_kind (v1.2.10 / ADR-009, task #20): 'scheduled' = the nightly batch;
-- 'on_demand' = the latest on-demand analysis outcome per source per day —
-- audit rows that never collide with (or overwrite) the daily rows.
CREATE TABLE pipeline_run (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_date    DATE        NOT NULL,
    source_name TEXT        NOT NULL,          -- 'yfinance'|'twse_tpex'|'edgar_13f'|'gdelt'
    status      TEXT        NOT NULL,          -- 'ok'|'unavailable'|'running'|'error'
    run_kind    TEXT        NOT NULL DEFAULT 'scheduled',   -- 'scheduled'|'on_demand'
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    message     TEXT,
    UNIQUE (run_date, source_name, run_kind)
);
CREATE INDEX ix_pipeline_run_date ON pipeline_run (run_date DESC);

-- user_config — single-row config (single-user app).
CREATE TABLE user_config (
    id             SMALLINT PRIMARY KEY DEFAULT 1,
    module_weights JSONB    NOT NULL DEFAULT '{"technical":0.30,"fundamental":0.30,"chip":0.25,"news":0.15}',
    horizon_months SMALLINT NOT NULL DEFAULT 6,
    ui_language    TEXT     NOT NULL DEFAULT 'en',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_singleton CHECK (id = 1),
    CONSTRAINT ck_horizon CHECK (horizon_months IN (3,6,12))
);
INSERT INTO user_config (id) VALUES (1) ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------
-- OPEN ITEM (domain contract, to reconcile w/ A2 Gherkin AC):
--   composite_call band thresholds. Deck §6 fixes Hold = [-0.75, +0.75] and
--   scale extremes ±2.0, but does not print the Strong/regular boundaries.
--   PROPOSED DEFAULT (pending A1/A2 confirmation):
--     STRONG_SELL [-2.00,-1.50) | SELL [-1.50,-0.75) | HOLD [-0.75,+0.75]
--     | BUY (+0.75,+1.50] | STRONG_BUY (+1.50,+2.00].
--   Thresholds live in the engine config, not the schema, so this does not block Foundation.
-- ---------------------------------------------------------------------
