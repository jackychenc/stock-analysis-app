# QA Test Slice — Task #9 chip adapters (Step 3): TWSE/TPEx + SEC EDGAR 13F

Owner: A6 · Prepared ahead of A5's candidate (per @Cindy). Scope = **raw chip/institutional FACT ingestion only** into `chip_data_tw` + `institutional_position_us`. NO signals/recommendation/weights (task #10), NO news (task #12). Binds to contract v1.2.5, RTM (FR-11/14/16, FR-52/53/54/56, FR-19, NFR-04/11), risks R-01 (source reliability) + R-04 (13F quarterly/delayed).
Reuses the task-#8 acceptance pattern; inherits A8's security bar (allowlist-before-egress, egress-pin, timeout+backoff+jitter, field-validity, log-hygiene, provenance). **Two sources → two adapters → two `pipeline_run` rows, independently isolated.**

## Source/table/key map
| Source | table | upsert key | source tag | market |
|---|---|---|---|---|
| TWSE/TPEx OpenAPI | `chip_data_tw` | `(ticker_id, trade_date)` | `twse_tpex` | `.TW`/`.TWO` only |
| SEC EDGAR 13F | `institutional_position_us` | `(ticker_id, quarter, filer_name)` **3-part** | `edgar_13f` | US only |

## Bucket 1 — SUCCESS
- **T9-S1 (TW)** TWSE/TPEx fetch → `chip_data_tw` rows: three-institution net, margin balance, block trades as decimal-safe NUMERIC; correct `trade_date`; `source='twse_tpex'`, `ingested_at` set.
- **T9-S2 (US)** EDGAR 13F fetch → `institutional_position_us` rows: `(quarter, filer_name, shares/value)` decimal-safe; `source='edgar_13f'`, `ingested_at` set.
- **T9-S3 market routing** — `.TW`/`.TWO` go ONLY to TWSE/TPEx (never EDGAR); US tickers go ONLY to EDGAR 13F (never TWSE). Assert no cross-market egress.
- **T9-S4 R-04 label (FR-16)** — US chip data labelled **"quarterly positioning"** (13F is quarterly + delayed) — the framing is present/persisted, not presented as daily/live.
- **T9-S5 pipeline** — `/pipeline/status` shows `twse_tpex` + `edgar_13f` each `ok` after a successful run (were `never_run`/`unavailable`).
- **T9-S6 multi-filer** — a US ticker with N distinct 13F filers in a quarter → N rows (one per `filer_name`), all under the same `(ticker_id, quarter)`.

## Bucket 2 — MISSING / INVALID
- **T9-M1** missing/null optional field (e.g. no block-trade data for a day, or a 13F filer with null value) → row persists with NULL; no crash.
- **T9-M2** per-field validity (A8-style): **impossible values rejected+counted** — negative share counts / negative margin-balance where impossible, NaN/inf → not persisted; **legitimately-signed values stored honestly** (three-institution NET can be negative = net sell; that's real, store it). Per-field spec, not a blanket sign filter.
- **T9-M3** malformed/partial payload → row rejected cleanly (logged, counted), no half-row that 500s a later read.

## Bucket 3 — OUTAGE / RATE-LIMIT (R-01, FR-53, NFR-04)
- **T9-O1** TWSE OpenAPI OR EDGAR failure (timeout/HTTP error/rate-limit) → **that source** marked `unavailable`; the **other chip source + yfinance + peers unaffected** (two independent chip adapters must not co-fail).
- **T9-O2** snapshot still written with the failed source flagged data-unavailable (FR-54).
- **T9-O3** per-source `consecutive_bad_days` 0→1→2 (per bad **run_date**, per #8 semantics) + recovery reset; alert threshold at 2 (R-01/NFR-11).
- **T9-O4** 429/5xx → explicit timeout + paced + exp-backoff-w-jitter → exhausted retries mark unavailable, **no retry storm**; non-transient fast-fail.

## Bucket 4 — DETERMINISTIC FIXTURE MODE (FR-19, CI safety)
- **T9-D1** fixture/recorded mode for BOTH sources → byte-reproducible; **no live TWSE/EDGAR network in CI**.
- **T9-D2** env/flag selects fixture vs live; `git grep` shows no unguarded live TWSE/EDGAR call in the test path.

## Bucket 5 — Batch integration (FR-52/56, D4)
- **T9-B1** idempotent re-ingest: `chip_data_tw` on `(ticker_id, trade_date)`, `institutional_position_us` on the **3-part `(ticker_id, quarter, filer_name)`** → re-run updates in place, **no duplicate rows**, `ingested_at` advances (last-write). (Watch the 3-part key — a bug that omits `filer_name` would collapse multiple filers into one row.)
- **T9-B2** both chip adapters run within the batch without blocking peers; golden `recommendation` rows untouched.

## Security-controls to co-verify with A8 (per-source)
- Egress pinned: `twse_tpex` reaches only TWSE/TPEx OpenAPI hosts; `edgar_13f` reaches only SEC EDGAR hosts; symbol/CIK allowlist before egress.
- **SEC EDGAR fair-access:** requests must carry a declared descriptive User-Agent (SEC policy) or EDGAR blocks — verify the adapter sets it (compliance/reliability, coordinate w/ A8).
- Log hygiene: source+ticker+status only, no raw response bodies. Provenance: `source` + `ingested_at` on insert AND update.

## RTM linkage
FR-11 (per-source availability) · FR-14 (chip signal INPUT facts: TW 3-inst/margin/block; US 13F) · FR-16 (US quarterly-positioning label) · FR-52/53/54/56 · FR-19 · NFR-04/11 · R-01/R-04. Gate dims: D4 (batch), D15 (data correctness), D1s (chip signal-input facts), D14 (observability).

## How I'll run it (when A5 posts a candidate)
1. Reproduce A5's unit/contract suite (both sources: success + missing/invalid + outage + fixture + 3-part-key idempotency + market-routing).
2. Live co-run on the stack (fixture mode): trigger ingestion → chip_data_tw + institutional_position_us persisted (decimal-safe, provenance), both sources `ok` in `/pipeline/status`, multi-filer rows correct.
3. Fault-injection per source (hosts-block, #8-style): TWSE down → `unavailable` while EDGAR+yfinance continue; EDGAR down → same; counter arc + recovery.
4. PASS/BLOCK verdict; mismatches = defects. Verify against `git show <declared-SHA>:` — NOT the working tree (lesson from #8 Y-1).
