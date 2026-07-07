# QA VERDICT — Task #6 Foundation (A5 build)

**Verdict:** ⚠️ PASS (code/test evidence) — **final PASS BLOCKED on live-stack smoke** (NFR-10b)
**Gate targeted:** G1 Foundation quality gates (FG1–FG6) → advances to critical-path (task #8)
**Reviewed by:** @SDLCQATestEngineerAgent · Date: 2026-07-08 · Contract: v1.2.1 (schema/OpenAPI from G1-signed v1.1)
**Build:** repo `projects/stock-app/` on Jacky HomeMac, commit `23eec46` (v1.2.1 adoption). Reviewed by direct inspection + running the suite.

## Results by Foundation gate
| FG | Check | Result | Evidence |
|---|---|---|---|
| FG1 | Schema migration/constraints | ✅ PASS (static) | `db/schema.sql`: pgcrypto ext; hypertables price_bar/technical_indicator/chip_data_tw; `ck_rec_breakdown_nonempty`; `ck_rec_suppressed_shape` (composite/targets NULL iff SUPPRESSED, else composite NOT NULL); composite_signal nullable NUMERIC(4,2). Live apply pending (DB). |
| FG2 | OpenAPI availability | ✅ PASS | `openapi.yaml` parses, 14 paths; full §21 surface (auth login/logout/refresh, stocks/{ticker}/dashboard+per-lens+backtest+supply-chain, recommendations/log+annotate, config/weights, pipeline/status). |
| FG3 | Auth happy + negative / suite | ✅ PASS | **26/26 tests pass — I reproduced independently** (`.venv/bin/pytest -q`). Incl. JWT access/refresh rotation, refresh-can't-be-access, weights-must-sum-to-1, dashboard renormalisation + suppression + insufficient-history. |
| FG4 | Docker / CI | ✅ PASS (static) | `docker-compose.yml` all host ports bound `127.0.0.1` (5432/6379/8000/3000; web opt-in profile). `.github/workflows/ci.yml`: ruff + pytest + migration-verification (apply+verify+seed). Can't execute remotely (no runner) — noted. |
| FG5 | Seed data | ✅ PASS (static) | `scripts/seed.py` + `app/db/migrate.py` present. Live seed pending DB. |
| FG6 | No credential leakage | ✅ PASS | `.env` gitignored; no tracked `.env`; no secret-shaped strings in tracked files; `.env.example` = empty placeholders w/ `token_hex(32)` gen instructions. |

## Contract-shape tests → my gate mapping (strong)
`tests/test_dashboard_contract.py` directly exercises my BLOCK gates:
- Always 4 module keys ok|unavailable (D3 / FR-57).
- 1 down → 200, module flagged, renormalised, `reduced_confidence=true`, unavailable `weight_effective=0`, survivors sum ≈1.0 (D2 / FR-34/36/37).
- ≥2 down → `composite_call=SUPPRESSED`, `suppressed_reason="Analysis Only — Insufficient Data"`, ck_rec_suppressed_shape (D2 / FR-35 — my BLOCK gate).
- insufficient_history (D5 / FR-42).

## BLOCKING condition (agreed w/ A1 + Cindy)
⛔ **Live-stack smoke not yet executed.** Localhost IS the deployment target (NFR-10b) → `docker compose up` on a real host is the acceptance environment; no Docker runtime on Jacky HomeMac yet. **Needs @jackychenc to authorize Docker/colima install.** Until then task #6 stays in_review.

### Live-stack smoke checklist (I run this the moment Docker is authorized)
1. `docker compose up` → all 4 services healthy, host ports bound to 127.0.0.1 only (verify `lsof -iTCP -sTCP:LISTEN`).
2. Migrate applies `db/schema.sql` (v1.2.1) clean from empty DB; hypertables + all CHECK constraints present; `seed.py` loads (.TW/.TWO/US tickers + config row).
3. `GET /healthz` 200 · `GET /api/v1/pipeline/status` returns per-source status.
4. Dashboard 3 states against seeded data: Normal / Reduced-Confidence / Analysis-Only(SUPPRESSED) — verify 200-with-flag, all-4-keys, suppressed shape.
5. Auth: `/auth/login` issues cookie(web)/JWT(iOS); `/auth/refresh` rotates; protected route 401 unauthenticated / expired.
6. FR-39 disclaimer present in payload/header on recommendation surfaces.
7. DB-level: attempt to write a SUPPRESSED row with a non-null composite → rejected by `ck_rec_suppressed_shape` (TC-SUPPRESS-SHAPE, live).

## Minor / non-blocking findings (fix opportunistically, not gating)
1. **CI label drift:** migration-verification job comment says "Apply schema v1.0" though repo adopted v1.2.1 — update label; confirm `migrate.py` targets the v1.2.1 `db/schema.sql`.
2. **weight_effective precision:** test fake-store (`conftest.py`) rounds to 6dp; contract §9 persists 2dp (0.43). Stub artifact — the REAL engine (task #10) must persist 2dp per contract + `golden_fixtures.json`. Flag for #10, not #6.
3. **JWT key length:** `InsecureKeyLengthWarning` from a short test key; ensure local/CI `JWT_SECRET` uses ≥32 bytes (`.env.example` already instructs it). Cosmetic.

## Recommendation to PM
Accept the code/test evidence as FG PASS; keep task #6 in_review with a single explicit blocker = live-stack smoke (Docker authorization). On a green live smoke, task #6 → done and task #8 (yfinance) starts. No S1/S2 defects found.
