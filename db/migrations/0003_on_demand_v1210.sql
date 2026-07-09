-- v1.2.10 / ADR-009 (task #20): on-demand analysis audit rows share
-- pipeline_run with the nightly batch, discriminated by run_kind —
-- 'scheduled' (the daily batch; existing rows default here) vs 'on_demand'
-- (latest on-demand outcome per source per day, upserted). Additive: the
-- unique key widens so on-demand rows never collide with — and can never
-- overwrite — the daily rows.

ALTER TABLE pipeline_run
    ADD COLUMN IF NOT EXISTS run_kind TEXT NOT NULL DEFAULT 'scheduled';

ALTER TABLE pipeline_run
    DROP CONSTRAINT IF EXISTS pipeline_run_run_date_source_name_key;
-- Fresh installs get the widened key straight from schema.sql (same
-- auto-generated name) — drop-then-add keeps this migration re-runnable
-- after either starting point.
ALTER TABLE pipeline_run
    DROP CONSTRAINT IF EXISTS pipeline_run_run_date_source_name_run_kind_key;
ALTER TABLE pipeline_run
    ADD CONSTRAINT pipeline_run_run_date_source_name_run_kind_key
        UNIQUE (run_date, source_name, run_kind);
