# Stock Investment Analysis App — Architecture Overview (C4) · v1.0

**Owner:** A3 Solution Architect · **Status:** G1 baseline freeze · **Source:** system design deck §18–23.
This ratifies the deck. Design principle: *boring, cheap, well-documented over scalable and exotic* — single user, once per day.

---

## C1 — System Context

```
        ┌──────────────┐         REST/HTTPS        ┌──────────────────────────┐
        │  User (1)    │  ───────────────────────► │ Stock Investment         │
        │ web + iOS    │  ◄─── snapshot reads ───── │ Analysis App             │
        └──────────────┘                            └────────────┬─────────────┘
                                                   daily batch pulls (once, ~03:00 TW)
                    ┌──────────────┬──────────────┬──────────────┴───┬──────────────┐
                    ▼              ▼              ▼                   ▼              ▼
                yfinance     TWSE/TPEx        SEC EDGAR            GDELT        curated wafer
              (price/fund)   OpenAPI (chip)   13F (US inst.)   (news+VADER)   supply-chain YAML
```

External deps are all **free-first** (R-01…R-04); paid feeds (TEJ/CMoney/Bloomberg) are a documented escalation, not MVP.

## C2 — Container view

| Container | Tech | Responsibility |
|---|---|---|
| Web client | Next.js/React/TS/Tailwind, TradingView Lightweight Charts | Desktop/browser surface; reads snapshot |
| iOS client | Swift/SwiftUI | Primary mobile; consumes same OpenAPI |
| API app | FastAPI (Python 3.12), Uvicorn | Auth-gated REST; serves precomputed snapshot; no live compute on request path |
| Batch job | APScheduler (dev) / Container Apps Job cron (prod) | Daily ingest→signal→score→backtest→persist |
| Relational store | PostgreSQL 16 + TimescaleDB | Facts (hypertables) + decisions + config |
| Cache | Redis | Read-path cache for dashboard |
| Secrets | Azure Key Vault + managed identity | Encryption key (NFR-05), no stored connection strings |

**Auth:** one FastAPI credential verifier; two token strategies — session cookie (web) + short-lived JWT + refresh (iOS). See ADR-002.

## C3 — Component view (API + Domain, deck §19)

```
FastAPI app
├── auth middleware ....... verify cookie|JWT -> principal
├── routers
│   ├── /stocks/*  ........ Read Service (dashboard = single-row snapshot read, Redis-cached)
│   ├── /recommendations/* . Read Service + annotate -> user_decision_log
│   ├── /config/*  ........ user_config read/write (weights, horizon)
│   └── /pipeline/status .. pipeline_run read
└── domain services (invoked mostly by batch, some read on request)
    ├── Recommendation Engine .. signals + weights -> composite, target, confidence, breakdown
    ├── Supply Chain Service ... graph read (discovery only, not scored)
    └── Backtest Engine ........ rolling accuracy, completeness-segmented

Batch pipeline (Container Apps Job)
  Adapters (yfinance | twse_tpex | edgar_13f | gdelt+vader)
     └─ per-source try/except -> pipeline_run.status ok|unavailable (never aborts peers)
  → Signal Calculators (per module -> [-2,+2])
  → Recommendation Engine (renormalise on missing; suppress if >=2 down)
  → Backtest Engine
  → Persist versioned daily snapshot (recommendation immutable)
```

## Key flows

**Daily batch (deck §22.1, SLA NFR-01 by 07:00 TW):** trigger 03:00 → ingest (isolated) → transform → score → backtest → persist snapshot. The 03:00 window captures prior full US session + TW evening chip data in one run.

**Failure resilience (deck §22.4):** one adapter fails → module `unavailable`, peers continue → renormalise effective weights, completeness < 1.0, flag *Reduced Confidence*. ≥2 modules down → **suppress** → "Analysis Only — Insufficient Data". Backtest tags partial-data days out of the headline number.

**Read path:** client → API → Redis (or Postgres single-row) → snapshot. No live computation ⇒ <5s dashboard (NFR-02).

---

## Deployment

### CURRENT TARGET — local host (testing) — per @jackychenc 2026-07-08
Build and run the whole system locally via **Docker Compose** (`docker-compose.yml`). This is the immediate deployment target and maps 1:1 to A5's Step 1 Foundation.

> **Topology update (ADR-008, task #19, 2026-07-08):** the **batch runs on the Mac HOST**, not in a container — the colima VM's egress IP is blocked by TPEx's Cloudflare WAF, so containerized egress can't reach the TW data sources; the host can. API + Postgres/Timescale + Redis + web stay containerized on loopback (127.0.0.1); the host batch (Python 3.12 + `uv`) connects to the containerized Postgres/Redis over loopback and reads the same secrets from a host `.env`. DB/Redis stay loopback-only (no widened exposure — the batch is on the same machine). Fixture-mode (no live egress) still runs in-container. **API/schema/domain-contract v1.2.6 unchanged — deployment topology only.**

| Service | Local (Docker Compose) |
|---|---|
| API + web | `api` container: FastAPI (Uvicorn) + Next.js dev/served build |
| Batch | `batch` container: APScheduler in-process cron (daily 03:00 TW; manual-trigger endpoint for test) |
| DB | `db` container: `timescale/timescaledb:pg16` |
| Cache | `redis` container |
| Secrets | `.env` (local) — `APP_ENCRYPTION_KEY` for pgcrypto; **NOT committed**. Placeholder in `.env.example`. |
| TLS | none locally (http://localhost); managed certs only in prod |

**Encryption at rest (NFR-05) locally:** pgcrypto column encryption stays; the symmetric key is read from `APP_ENCRYPTION_KEY` env var instead of Key Vault. The code path is identical — only the key *source* differs between local and cloud (single `KeyProvider` abstraction; see ADR-004).

### FUTURE TARGET — Azure PaaS v1.2 (deck §16, deferred, not this phase)
Kept as the documented production path; nothing in the code/contract blocks it (platform-agnostic REST + KeyProvider abstraction). Summary retained for planning:
Azure Japan East, App Service B2 Always-On (shared image: Next.js + FastAPI), Container Apps Job (nightly cron, 6h timeout), PostgreSQL Flexible Server B2s (Private Endpoint/VNet), Cache for Redis C0, Key Vault + managed identity, automated backups + PITR, App Insights + managed TLS, GitHub Actions CI/CD. ~$84–89/mo, budget alert $80.

> The earlier ~$20/mo single-VM Docker option and Azure PaaS are both **superseded for now** by local-host testing. Azure remains the future prod target pending client go.

### Security hardening backlog — TLS / non-loopback phase (per A8 secure-SDLC gate, 2026-07-08)
These apply when the app leaves loopback (TLS/prod). Tracked here for the migration; A8 owns them in the security register. NOT required for localhost testing:
- Cookie `Secure` flag (+ `SameSite`, `HttpOnly`) once served over HTTPS.
- Security headers: CSP, `X-Content-Type-Options: nosniff`, `X-Frame-Options`, HSTS.
- Refresh-token **server-side revocation** (local uses stateless rotation; prod needs a revocation store — Redis is already in the stack).
- Commit web lockfile + `npm ci` in CI (web arrives roadmap step 8).
- CI security scanning (gitleaks / pip-audit / npm audit / bandit or CodeQL) — A8 P1-SEC-2, wired now with the private-repo CI (A7-owned).
Localhost P1 (A8): fail-closed `JWT_SECRET` (refuse boot if <32 bytes or dev-default) — A5 implementation fix, not a contract change.

---

## ADR summary (full log in `adr-log.md`)

Per A1's directive, ADRs *ratify* the deck; none of the deck's decisions are contested.

| ADR | Decision | Status |
|---|---|---|
| 001 | Boring batch monolith; **no Kafka/CQRS/event-sourcing** | Accepted (ratifies deck) |
| 002 | Split auth: cookie (web) + JWT+refresh (iOS), one verifier | Accepted |
| 003 | `recommendation` is the immutable history log; decisions annotate separately | Accepted |
| 004 | Encrypt sensitive data at rest: pgcrypto column + Key Vault key + TDE | Accepted |
| 005 | Native iOS (not PWA); platform-agnostic REST so backend unchanged | Accepted |
| 006 | Azure PaaS footprint (supersedes $20/mo single-VM option) | Accepted |
| 007 | English-only, on-demand lookup, no watchlist entity (smallest MVP) | Accepted |

## Open items (non-blocking for Foundation; tracked for domain-contract phase)
1. **5-point band thresholds** — ✅ RESOLVED: ±1.5 split accepted (A2 locked FR-26, A1 seconded). See `domain-contract.md` §5.
2. **OD-2** `target_base` valuation blend method (engine step 4) — A2/domain + A3.
3. **OD-3** confidence formula reconcile with A2 Gherkin AC.
4. Human confirms (per A1, non-blocking): deck v1.0 client-final? default horizon 6mo / weights 30/30/25/15? (Azure/cost deferred — localhost-first per @jackychenc.)
