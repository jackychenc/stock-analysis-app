# Architecture Decision Log — Stock Investment Analysis App · v1.0

Per A1 PM: ADRs *ratify* the deck; raised only to record rationale, not to contest. Format: lightweight.

---

## ADR-001 — Boring batch monolith; no streaming/event backbone
**Status:** Accepted · **Context:** single user, one recommendation per ticker per day, reads touch only last night's snapshot. **Decision:** a modular monolith (FastAPI app + one daily batch job) over Postgres — *no Kafka, no CQRS, no event-sourcing, no low-latency infrastructure*. **Consequences:** minimal ops/cost; the request path does zero live computation (supports NFR-02 <5s). Revisit only if the app becomes multi-user or intraday.

## ADR-002 — Split authentication, single verifier
**Status:** Accepted · **Context:** two clients (Next.js web, native iOS). **Decision:** session cookie for web, short-lived JWT + refresh for iOS; one FastAPI credential verifier, two token-issuance strategies. **Consequences:** each client uses its platform-idiomatic session model; one code path validates principals. `/auth/refresh` for iOS only.

## ADR-003 — `recommendation` is the immutable history log
**Status:** Accepted · **Context:** need auditability of what was recommended and what the user did. **Decision:** `recommendation` rows are write-once (never UPDATE); user actions live in `user_decision_log` and only *annotate*. `methodology_version` stamped on every row. **Consequences:** clean separation of model output vs user behaviour; backtests and audits are reproducible. Enforced by convention + no UPDATE grants in prod.

## ADR-004 — Encrypt sensitive data at rest via a KeyProvider abstraction
**Status:** Accepted · **Context:** transaction prices and decision notes are personal financial data (NFR-05). **Decision:** pgcrypto column-level encryption (`pgp_sym_encrypt`) for `transaction_price`/`notes`; the symmetric key is resolved through a single `KeyProvider` interface — **local:** `APP_ENCRYPTION_KEY` env var; **prod:** Azure Key Vault + managed identity + DB TDE (defense-in-depth). **Consequences:** identical crypto code across environments; only the key *source* changes, so local-host testing and future Azure prod share one path.

## ADR-005 — Native iOS (not PWA); platform-agnostic REST
**Status:** Accepted · **Context:** client explicitly chose native iOS. **Decision:** build the REST API platform-agnostic; iOS (SwiftUI) and web (Next.js) consume the same OpenAPI. **Consequences:** backend is unchanged across clients; web remains for desktop.

## ADR-006 — Deployment: local host now, Azure PaaS as future prod
**Status:** Accepted (updated 2026-07-08 per @jackychenc) · **Context:** client directed local-host testing as the current target. **Decision:** Docker Compose local stack (timescaledb-pg16 + redis + api + batch) is the current deployment; Azure PaaS v1.2 (deck §16) is retained as the documented future prod path. **Consequences:** nothing in code/contract binds to Azure services (KeyProvider abstraction + no cloud-only APIs), so the cloud move is later config, not a rebuild.

## ADR-007 — English-only, on-demand lookup, no watchlist
**Status:** Accepted · **Context:** smallest MVP that meets the ask. **Decision:** no i18n framework (`ui_language` column kept for future), no watchlist entity; tickers looked up on demand. **Consequences:** less surface to build/test; both are additive later without schema rework.

## ADR-008 — Hybrid local topology: batch on host, API/DB/web in containers
**Status:** Accepted (2026-07-08, @jackychenc-approved, task #19) · **Context:** the colima VM's egress/NAT IP is blocked by TPEx's Cloudflare WAF, so the ingestion batch cannot reach the TW data sources (TWSE-rwd/TPEx) from *inside* a container; the Mac host reaches them fine (A7 pre-flight confirmed). **Decision:** run the **daily batch process directly on the Mac host** (Python 3.12 + `uv`, user-scope Homebrew) for its egress; keep **API + Postgres/Timescale + Redis + web containerized on loopback** (127.0.0.1). The host batch connects to the containerized Postgres over loopback (`127.0.0.1:5432`) and reads secrets (`APP_ENCRYPTION_KEY`, etc.) from a host `.env`. **Consequences:** bypasses the Cloudflare egress block without weakening the security posture — DB/Redis/API stay loopback-only, the host batch is on the same machine. Provenance/write path unchanged (same Postgres, same `pipeline_run`/`recommendation`). **API / schema / domain-contract v1.2.6 are UNCHANGED — this is deployment topology only.** The KeyProvider abstraction (ADR-004) already supports host env-var key resolution. NFR-10b "localhost-prod" requirement text owned by A2. Fixture-mode (no live egress) still runs fully in-container, so this does not affect the task #10 gate.

---

### Open decisions (deferred to domain-contract phase, do not block Foundation)
- **OD-1 — 5-point call band thresholds.** Deck fixes Hold=[-0.75,+0.75] and extremes ±2.0; Strong/regular boundaries unspecified. Proposed default ±1.5. Owner: A3 + A2 (reconcile vs Gherkin AC). Lives in engine config, not schema.
