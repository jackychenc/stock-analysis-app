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
# Task #20: never start the in-process analysis worker under tests — the
# background queue consumer would race route-level job-state assertions (and
# reach for a live Redis). Worker behavior is tested directly.
os.environ.setdefault("ANALYSIS_WORKER_ENABLED", "false")

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
        if "from technical_indicator" in q:  # task #14 technical lens detail
            rows = [r for r in self.store.get("technical_indicators", [])
                    if r["ticker_id"] == args[0]]
            return max(rows, key=lambda r: r["calc_date"]) if rows else None
        if "from fundamental" in q:  # task #14 fundamental lens detail
            rows = [r for r in self.store.get("fundamentals", [])
                    if r["ticker_id"] == args[0]]
            return max(rows, key=lambda r: r["asof_date"]) if rows else None
        if "from pipeline_run" in q and "order by run_date desc" in q:
            # task #14 news lens detail: latest scheduled gdelt fetch evidence
            runs = [r for r in self.store["pipeline_runs"]
                    if r["source_name"] == "gdelt"
                    and r.get("run_kind", "scheduled") == "scheduled"]
            return max(runs, key=lambda r: r["run_date"]) if runs else None
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
        if "from price_bar" in q and "order by bar_date desc" in q:
            # task #14 technical lens detail: newest N bars for one ticker
            bars = [b for b in self.store.get("price_bars", [])
                    if b.get("ticker_id") == args[0]]
            return sorted(bars, key=lambda b: b["bar_date"], reverse=True)[: args[1]]
        if "from price_bar" in q:  # task #13 backtest bar load
            return [b for b in self.store.get("price_bars", []) if b["bar_date"] <= args[0]]
        if "from chip_data_tw" in q:  # task #14 chip lens detail (TW)
            rows = [r for r in self.store.get("chip_tw", [])
                    if r["ticker_id"] == args[0]]
            return sorted(rows, key=lambda r: r["trade_date"], reverse=True)[: args[1]]
        if "filer_count" in q:  # task #14 chip lens detail (US 13F aggregates)
            rows = [r for r in self.store.get("us_positions", [])
                    if r["ticker_id"] == args[0] and r.get("shares") is not None]
            by_quarter: dict = {}
            for r in rows:
                agg = by_quarter.setdefault(
                    r["quarter"],
                    {"quarter": r["quarter"], "total_shares": 0, "filer_count": 0},
                )
                agg["total_shares"] += r["shares"]
                agg["filer_count"] += 1
            ordered = sorted(by_quarter.values(),
                             key=lambda a: a["quarter"], reverse=True)
            return ordered[: args[1]]
        if "from news_item" in q:  # task #14 news lens detail window
            rows = [r for r in self.store.get("news_items", [])
                    if r["ticker_id"] == args[0]
                    and args[1] <= r["published_at"] < args[2]]
            return sorted(rows, key=lambda r: r["published_at"],
                          reverse=True)[: args[3]]
        if "r.rec_date >= $1" in q:  # task #13 backtest rec load (windowed)
            rows = []
            for r in self.store["recommendations"]:
                if not (args[0] <= r["rec_date"] <= args[1]):
                    continue
                if len(args) > 2 and r["ticker_id"] != args[2]:
                    continue
                full_symbol = next(t["full_symbol"] for t in self.store["tickers"].values()
                                   if t["id"] == r["ticker_id"])
                rows.append({**r, "full_symbol": full_symbol})
            return rows
        if "from recommendation" in q:
            return []
        if "from ticker" in q:  # task #13 benchmark ticker-id map
            return list(self.store["tickers"].values())
        if "from schema_migrations" in q:
            return []
        raise AssertionError(f"FakeConnection.fetch: unhandled query: {q[:120]}")

    async def fetchval(self, query: str, *args):
        q = " ".join(query.split()).lower()
        if "min(rec_date)" in q:  # task #13 history gate
            recs = self.store["recommendations"]
            return min((r["rec_date"] for r in recs), default=None)
        if "max(rec_date)" in q:  # task #20 /analyze freshness check
            recs = [r["rec_date"] for r in self.store["recommendations"]
                    if r["ticker_id"] == args[0]]
            return max(recs, default=None)
        if "count(*) from ticker" in q:  # task #20 FR-61 pool-cap count
            assert "is_covered" in q  # benchmarks must never count
            return sum(1 for t in self.store["tickers"].values() if t["is_covered"])
        if "select score from institutional_position_us" in q:  # task #14
            rows = [r for r in self.store.get("us_positions", [])
                    if r["ticker_id"] == args[0] and r.get("score") is not None]
            return (max(rows, key=lambda r: r["quarter"])["score"]
                    if rows else None)
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


class FakeRedis:
    """Tiny in-memory redis.asyncio stand-in (task #20) — only the ops the
    on-demand job store/queue uses: hashes, strings, one list + blpop. Tests
    never need a live Redis."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def hset(self, key: str, mapping: dict):
        self.hashes.setdefault(key, {}).update(
            {k: str(v) for k, v in mapping.items()}
        )

    async def hget(self, key: str, field: str):
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key: str) -> dict:
        return dict(self.hashes.get(key, {}))

    async def set(self, key: str, value):
        self.strings[key] = str(value)

    async def get(self, key: str):
        return self.strings.get(key)

    async def delete(self, *keys: str):
        for key in keys:
            self.hashes.pop(key, None)
            self.strings.pop(key, None)
            self.lists.pop(key, None)

    async def rpush(self, key: str, value):
        self.lists.setdefault(key, []).append(str(value))

    async def blpop(self, key: str, timeout: float = 0):
        import asyncio

        items = self.lists.get(key)
        if items:
            return key, items.pop(0)
        await asyncio.sleep(0)  # yield to peers; never actually block a test
        return None

    async def aclose(self):
        return None


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
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def client(monkeypatch, store, fake_redis) -> TestClient:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USER)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(TEST_PASSWORD))
    get_settings.cache_clear()

    fake = FakePool(store)

    async def fake_get_pool():
        return fake

    monkeypatch.setattr(pool_module, "get_pool", fake_get_pool)
    # Routers imported get_pool by reference; patch their module globals too.
    import app.api.routers.analyze as analyze_router
    import app.api.routers.config as config_router
    import app.api.routers.pipeline as pipeline_router
    import app.api.routers.recommendations as rec_router
    import app.api.routers.stocks as stocks_router

    for mod in (stocks_router, rec_router, config_router, pipeline_router,
                analyze_router):
        monkeypatch.setattr(mod, "get_pool", fake_get_pool)

    # Task #20: the analyze routes talk to Redis — same by-reference patching.
    import app.db.redis as redis_module

    async def fake_get_redis():
        return fake_redis

    for mod in (redis_module, analyze_router):
        monkeypatch.setattr(mod, "get_redis", fake_get_redis)

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
