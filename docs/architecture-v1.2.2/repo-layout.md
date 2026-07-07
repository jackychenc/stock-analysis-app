# Repo Layout — Stock Investment Analysis App · v1.0 (Foundation contract)

Proposed monorepo structure for A5's Step 1 Foundation. Backend-first; web and iOS consume the same OpenAPI.

```
stock-analysis-app/
├── docker-compose.yml            # local-host stack (db + redis + api + batch)
├── Dockerfile                    # shared image: FastAPI api + batch entrypoints
├── .env.example                  # POSTGRES_*, APP_ENCRYPTION_KEY, JWT_SECRET (no real values)
├── pyproject.toml                # Python 3.12, FastAPI, SQLAlchemy 2, psycopg, pydantic v2
├── openapi.yaml                  # API contract v1.0 (authoritative; also served by FastAPI)
├── db/
│   └── schema.sql                # DDL v1.0 (Timescale + pgcrypto); loaded on db init
├── app/
│   ├── main.py                   # FastAPI app factory, router mounts, disclaimer middleware
│   ├── config.py                 # settings from env (DATABASE_URL, REDIS_URL, keys, ENV)
│   ├── db.py                     # engine/session, Redis client
│   ├── security/
│   │   ├── auth.py               # one verifier; cookie (web) + JWT+refresh (iOS)  [ADR-002]
│   │   └── keyprovider.py        # env (local) | Key Vault (prod) key source       [ADR-004]
│   ├── models/                   # SQLAlchemy models mirroring schema.sql
│   ├── schemas/                  # pydantic request/response models (match openapi.yaml)
│   ├── routers/
│   │   ├── stocks.py             # /stocks/*  (dashboard = single-row read)
│   │   ├── recommendations.py    # /recommendations/log (+ annotate)
│   │   ├── config.py             # /config/weights
│   │   ├── pipeline.py           # /pipeline/status (+ /pipeline/run-now for local test)
│   │   └── auth.py               # /auth/login|logout|refresh
│   ├── services/
│   │   ├── read_service.py       # snapshot reads, Redis caching
│   │   ├── recommendation_engine.py  # composite, target, confidence, breakdown, renormalise/suppress
│   │   ├── supply_chain.py       # discovery graph (not scored)
│   │   └── backtest.py           # rolling accuracy, completeness-segmented
│   └── batch/
│       ├── scheduler.py          # APScheduler daily 03:00 TW
│       ├── pipeline.py           # orchestrator: ingest→signal→score→backtest→persist
│       ├── adapters/             # yfinance, twse_tpex, edgar_13f, gdelt_vader (per-source isolation)
│       └── signals/              # technical, fundamental, chip, news calculators (-2..+2)
├── web/                          # Next.js/React/TS/Tailwind (roadmap step 8)
├── ios/                          # SwiftUI (roadmap step 8b, parallel)
└── tests/
    ├── contract/                 # OpenAPI schema conformance (A6 hook)
    └── unit/                     # scoring/backtest golden fixtures (decimal-safe, TDD money paths)
```

## Foundation (Step 1) acceptance
- `docker compose up` → db initialises `schema.sql`, api healthy at `http://localhost:8000`, `/docs` serves OpenAPI.
- `/auth/login` issues cookie (web) / JWT (iOS); protected route rejects unauthenticated.
- Empty-state `/stocks/{t}/dashboard` returns the contract shape (all 4 module keys, `unavailable` until batch runs) or `SECTOR_NOT_COVERED`.
- CI (GitHub Actions): lint + `pytest` (contract + unit) on push.

## Contract authority
`openapi.yaml` + `db/schema.sql` in this repo are the **authoritative v1.0 contract** (this freeze). A5 diffs the scaffold against them and adopts; drift is fixed on the implementation side, tracked via RTM change control.
