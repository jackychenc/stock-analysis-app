# Stock Investment Analysis App

Five-lens holistic equity analysis with explainable Buy/Sell recommendations.
Single-user decision-support tool — **not financial advice** (FR-39).

- Markets: TWSE (`.TW`) · TPEx (`.TWO`) · US
- Four scoring lenses (Technical 30 / Fundamental 30 / Chip 25 / News 15, −2…+2)
  → one composite call + Bear/Base/Bull target + confidence; supply-chain lens
  is discovery-only. Missing-data honesty: 1 module down → renormalise +
  Reduced Confidence; ≥2 down → "Analysis Only — Insufficient Data".
- Single daily batch (03:00 TW, done by 07:00 — NFR-01); clients read
  precomputed snapshots only (dashboard <5s — NFR-02).

**Contract authority:** `openapi.yaml` + `db/schema.sql` (A3 contract freeze
v1.0, `deployment=localhost-testing-first`). Changes go through RTM change
control.

## Local setup (current deployment target)

Prereqs: Docker (Compose v2). Then:

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # → JWT_SECRET
python3 -c "import secrets; print(secrets.token_hex(32))"   # → APP_ENCRYPTION_KEY
# set ADMIN_USERNAME; generate ADMIN_PASSWORD_HASH:
uv run python scripts/hash_password.py                       # paste into .env

docker compose up --build
```

- API: <http://localhost:8000> — interactive OpenAPI at `/docs`, liveness at `/healthz`
- Web: <http://localhost:3000>
- DB schema loads automatically on first `db` init (`db/schema.sql`)
- Seed tickers: `docker compose exec api python scripts/seed.py`
- Trigger the batch skeleton on demand: `POST /api/v1/pipeline/run-now` (auth required)

### Without Docker (backend dev loop)

```bash
uv sync                      # Python 3.12 env
uv run pytest -q             # unit + contract tests (no DB needed)
uv run ruff check .          # lint
# with a local Postgres+TimescaleDB running:
uv run python -m app.db.migrate && uv run python scripts/seed.py
uv run uvicorn app.main:app --reload
```

## Repo layout

Per A3 `repo-layout.md` v1.0: `app/` (FastAPI + batch), `db/schema.sql`,
`web/` (Next.js, Step 8), `ios/` (SwiftUI, Step 8b), `tests/`, `openapi.yaml`.

## Auth

Single user; one credential verifier, two strategies (ADR-002):
web `POST /api/v1/auth/login` (`client:"web"`) → HttpOnly `session` cookie;
iOS (`client:"ios"`) → short-lived JWT + refresh (`POST /auth/refresh`).

## Security notes (NFR-05)

- `transaction_price` / `notes` in the decision log are encrypted column-level
  via pgcrypto (`pgp_sym_encrypt`) with `APP_ENCRYPTION_KEY` (env-sourced
  locally; KeyProvider abstraction for a future cloud phase).
- No secrets in the repo — `.env` is gitignored; `.env.example` carries names only.

## Roadmap state

This is **Step 1 Foundation** (task #6): scaffold, schema, auth, contract-shaped
route stubs, batch skeleton, CI. Ingestion (Steps 2/3/6), the recommendation
engine (4), supply-chain graph (5), backtest (7), full web/iOS UI (8/8b) and
ops hardening (9) are separate roadmap tasks and NOT implemented here yet.
