"""Test fixtures.

Unit/contract tests run WITHOUT a live database: DB access goes through
app.db.pool.get_pool, which tests monkeypatch with FakePool. Integration
against real Timescale runs via docker compose (see README).
"""

import json
import os
from datetime import date

import pytest
from fastapi.testclient import TestClient

# 64 hex chars — satisfies the fail-closed >=32-byte JWT_SECRET boot check.
# app/main.py builds the module-level `app` at import time and refuses a weak
# secret, so this MUST be set before any app.main import below.
TEST_JWT_SECRET = "0123456789abcdef" * 4
os.environ.setdefault("JWT_SECRET", TEST_JWT_SECRET)

import app.db.pool as pool_module  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import create_app  # noqa: E402

TEST_USER = "jacob"
TEST_PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _test_env(monkeypatch):
    """Every test gets a boot-valid JWT_SECRET (create_app fails closed without one)."""
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeConnection:
    """Minimal asyncpg-compatible fake keyed on SQL substrings."""

    def __init__(self, store: dict):
        self.store = store

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split()).lower()
        if "from ticker where" in q:
            symbol = str(args[0]).upper()
            return self.store["tickers"].get(symbol)
        if "from recommendation" in q:
            ticker_id = args[0]
            recs = [r for r in self.store["recommendations"] if r["ticker_id"] == ticker_id]
            return max(recs, key=lambda r: r["rec_date"]) if recs else None
        if "from user_config" in q:
            return self.store["user_config"]
        if "max(run_date)" in q:
            runs = self.store["pipeline_runs"]
            return {"run_date": max((r["run_date"] for r in runs), default=None)}
        if "insert into user_decision_log" in q:
            return None  # no matching recommendation in fake store
        raise AssertionError(f"FakeConnection.fetchrow: unhandled query: {q[:120]}")

    async def fetch(self, query: str, *args):
        q = " ".join(query.split()).lower()
        if "from pipeline_run" in q:
            return []
        if "from recommendation" in q:
            return []
        if "from schema_migrations" in q:
            return []
        raise AssertionError(f"FakeConnection.fetch: unhandled query: {q[:120]}")

    async def fetchval(self, query: str, *args):
        return 1

    async def execute(self, query: str, *args):
        q = " ".join(query.split()).lower()
        if "update user_config" in q:
            self.store["user_config"] = {
                "module_weights": args[0],
                "horizon_months": args[1],
            }
        return "OK"


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, store: dict):
        self.conn = FakeConnection(store)

    def acquire(self):
        return _Acquire(self.conn)


@pytest.fixture
def store() -> dict:
    default_weights = json.dumps(
        {"technical": 0.30, "fundamental": 0.30, "chip": 0.25, "news": 0.15}
    )
    return {
        "tickers": {
            "2330.TW": {
                "id": 1, "full_symbol": "2330.TW", "symbol": "2330", "exchange": "TWSE",
                "name": "TSMC", "sector": "Semiconductors", "is_covered": True,
            },
            "AAPL": {
                "id": 2, "full_symbol": "AAPL", "symbol": "AAPL", "exchange": "US",
                "name": "Apple", "sector": "Technology", "is_covered": True,
            },
            "XXXX.TW": {
                "id": 3, "full_symbol": "XXXX.TW", "symbol": "XXXX", "exchange": "TWSE",
                "name": "Uncovered Corp", "sector": "Shipping", "is_covered": False,
            },
        },
        "recommendations": [],
        "pipeline_runs": [],
        "user_config": {"module_weights": default_weights, "horizon_months": 6},
    }


@pytest.fixture
def client(monkeypatch, store) -> TestClient:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USER)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(TEST_PASSWORD))
    get_settings.cache_clear()

    fake = FakePool(store)

    async def fake_get_pool():
        return fake

    monkeypatch.setattr(pool_module, "get_pool", fake_get_pool)
    # Routers imported get_pool by reference; patch their module globals too.
    import app.api.routers.config as config_router
    import app.api.routers.pipeline as pipeline_router
    import app.api.routers.recommendations as rec_router
    import app.api.routers.stocks as stocks_router

    for mod in (stocks_router, rec_router, config_router, pipeline_router):
        monkeypatch.setattr(mod, "get_pool", fake_get_pool)

    test_app = create_app()
    with TestClient(test_app, raise_server_exceptions=False) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def web_client(client: TestClient) -> TestClient:
    """Client logged in via the web cookie strategy."""
    r = client.post(
        "/api/v1/auth/login",
        json={"username": TEST_USER, "password": TEST_PASSWORD, "client": "web"},
    )
    assert r.status_code == 200
    return client


def sample_breakdown(*, unavailable: tuple[str, ...] = ()) -> list[dict]:
    weights = {"technical": 0.30, "fundamental": 0.30, "chip": 0.25, "news": 0.15}
    scores = {"technical": 1.2, "fundamental": 0.8, "chip": 1.5, "news": -0.5}
    live = {m: w for m, w in weights.items() if m not in unavailable}
    total = sum(live.values())
    return [
        {
            "module": m,
            "signal_score": None if m in unavailable else scores[m],
            "weight_assigned": w,
            # domain-contract v1.2.2 §2/§9: full precision for computation,
            # 4dp for storage/display of weight_effective.
            "weight_effective": 0.0 if m in unavailable else round(w / total, 4),
            "status": "unavailable" if m in unavailable else "ok",
        }
        for m, w in weights.items()
    ]


def make_recommendation(ticker_id: int = 1, *, unavailable: tuple[str, ...] = ()) -> dict:
    suppressed = len(unavailable) >= 2
    return {
        "ticker_id": ticker_id,
        "rec_date": date(2026, 7, 7),
        # Contract v1.1+ (ck_rec_suppressed_shape): NULL iff SUPPRESSED.
        "composite_signal": None if suppressed else 0.94,
        "composite_call": "SUPPRESSED" if suppressed else "BUY",
        "target_price_bear": None if suppressed else 850.0,
        "target_price_base": None if suppressed else 1000.0,
        "target_price_bull": None if suppressed else 1150.0,
        "confidence_level": None if suppressed else "HIGH",
        "confidence_pct": None if suppressed else 82.5,
        "conflict_flag": False,
        "reduced_confidence": len(unavailable) == 1,
        "horizon_months": 6,
        "per_module_breakdown": json.dumps(sample_breakdown(unavailable=unavailable)),
        "data_completeness": (4 - len(unavailable)) / 4,
        "methodology_version": "mvp-1.0",
    }
