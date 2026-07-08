# NFR Budgets & Validation — Stock Investment Analysis App · v1.0

Owner: A3. Quantifies the deck's named NFRs + fills gaps. A2 owns the authoritative NFR catalogue; this is the architecture budget/validation view. Targets apply to **local-host testing** now; cloud targets noted where they differ.

| ID | NFR | Budget / Target | How enforced | How validated |
|---|---|---|---|---|
| NFR-01 | Daily snapshot ready by **07:00 TW** | Batch starts 03:00 TW; must finish ≤ 4h (hard cap 6h job timeout). | APScheduler 03:00 trigger; per-source timeouts; `pipeline_run` timestamps; `sla_met` on `/pipeline/status`. | Timed batch run on a full ticker set; assert `completed_at ≤ 07:00 TW`. Cloud: pre-launch load test on B2s. |
| NFR-02 | Dashboard loads **< 5s** | p95 API response < 800ms (single-row read); client render budget the rest. | No live compute on read path; Redis cache; single-row `recommendation` fetch + 4 module summaries. | k6/locust: GET `/stocks/{t}/dashboard` p95 < 800ms warm cache, < 1.5s cold. |
| NFR-05 | Encrypt sensitive data at rest | `transaction_price`, `notes` encrypted; key never in DB/repo. | pgcrypto `pgp_sym_encrypt`; key via `KeyProvider` (env local / Key Vault prod); TDE in prod. | Inspect raw table → bytea unreadable without key; key-rotation drill. |
| FR-39 | Persistent "Not financial advice" disclaimer | Present on every recommendation surface. | Rendered by client on all rec views; server includes disclaimer text/flag in payload. | UI/contract test: disclaimer present on dashboard, log, backtest. |

## Additional architecture budgets

| Area | Target |
|---|---|
| Availability | Single-user, batch app — no HA requirement in MVP. Read path degrades to Postgres if Redis down. Total outage → 503 (reserved). |
| Correctness / resilience | One adapter failure never aborts peers (per-source try/except). ≥2 scoring modules down → suppress. Backtest keeps partial-data days out of headline. |
| Data freshness | Snapshot is "as of last completed batch"; `/pipeline/status` exposes staleness + `consecutive_bad_days` (R-01 alert after 2). |
| Backup / recovery | **Local:** named Docker volume + `pg_dump` on demand. **Prod:** Flexible Server automated backups + PITR. RPO ≤ 24h (data is reproducible from sources anyway), RTO ≤ 1 batch cycle. |
| Cost guardrail | **Local:** ~$0. **Prod (future):** ≤ $80/mo budget alert. |
| Observability | `/pipeline/status` (per-source status, SLA met, bad-day counter). Prod adds App Insights 1GB/day. |
| Security | Auth-gated routes; secrets via env/Key Vault; no secrets in repo (`.env.example` only); TLS in prod. |

## Backfill note
Backtest engine (roadmap step 7) needs **≥12–24 months** of history; until backfilled, `/stocks/{t}/backtest` returns `insufficient_history=true` rather than a misleading number.
