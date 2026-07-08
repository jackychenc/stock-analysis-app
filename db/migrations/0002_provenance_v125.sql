-- v1.2.5 (A3 ruling 2026-07-08): row-level provenance on raw-fact tables.
-- Additive, non-breaking; upserts set ingested_at = now() on update
-- (last-fetched semantics, not first-seen).

ALTER TABLE price_bar
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE fundamental
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'yfinance',
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE chip_data_tw
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'twse_tpex',
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE institutional_position_us
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'edgar_13f',
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE news_item
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT now();
