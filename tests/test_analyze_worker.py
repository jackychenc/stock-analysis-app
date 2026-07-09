"""Task #20 worker + plumbing (ADR-009 v1.2.10): status transitions
queued→running(fetching)→running(scoring)→terminal, coverage-pool promotion +
market routing, the sanitized failure categories (SEC-ONDEMAND-ERROR-HYGIENE:
injected upstream HTML never reaches the job hash), the §4a override
threading, the run_kind='on_demand' audit upsert, and the 0003 migration /
schema mirror. No live Redis/Postgres anywhere."""

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.batch.adapters.common import AdapterUnavailable
from app.batch.pipeline import write_on_demand_audit
from app.services import analysis_jobs
from app.services.recommendation_engine import MODULES, compute_recommendation
from tests.conftest import FakeRedis

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_DATE = date(2026, 7, 9)
SOURCES = ("yfinance", "twse_tpex", "edgar_13f", "gdelt")
HTML_ERROR = "<html><body>502 Bad Gateway — internal token abc123</body></html>"


class RecordingRedis(FakeRedis):
    """FakeRedis + a (status, phase) transition log for ordering assertions."""

    def __init__(self):
        super().__init__()
        self.transitions: list[tuple[str, str | None]] = []

    async def hset(self, key, mapping):
        await super().hset(key, mapping)
        if "status" in mapping:
            self.transitions.append((mapping["status"], mapping.get("phase")))


class JobConn:
    """Captures the ticker promotion + audit SQL run_job issues."""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))

    async def fetchrow(self, query, *args):
        assert "FROM ticker" in query
        return {"id": 1, "full_symbol": args[0], "exchange": "US"}


def ok_outcomes(**over):
    outcomes = {s: ("ok", f"[fixture] {s} stats") for s in SOURCES}
    outcomes.update(over)
    return outcomes


def engine_result(*, unavailable: tuple[str, ...] = ()):
    signals = {m: (None if m in unavailable else Decimal("1")) for m in MODULES}
    return compute_recommendation(signals)


def wire(monkeypatch, *, outcomes=None, fetch_exc=None, result=None,
         score_exc=None):
    """Monkeypatch the batch seams run_job reuses (fetch, audit, weights,
    score) — the worker logic itself runs for real."""
    seen: dict = {"audit": [], "override": "UNSET"}

    async def fake_fetch(conn, symbol):
        if fetch_exc is not None:
            raise fetch_exc
        return outcomes if outcomes is not None else ok_outcomes()

    async def fake_audit(conn, run_date, o):
        seen["audit"].append((run_date, o))

    async def fake_weights(conn):
        return ({}, 6)

    async def fake_score(conn, ticker, rec_date, weights, horizon, version,
                         news_fetch_override=None):
        seen["override"] = news_fetch_override
        if score_exc is not None:
            raise score_exc
        return result

    monkeypatch.setattr(analysis_jobs, "run_on_demand_fetch", fake_fetch)
    monkeypatch.setattr(analysis_jobs, "write_on_demand_audit", fake_audit)
    monkeypatch.setattr(analysis_jobs, "_load_weights", fake_weights)
    monkeypatch.setattr(analysis_jobs, "score_ticker", fake_score)
    return seen


async def run_one(monkeypatch, ticker="TSLA", **wiring):
    r = RecordingRedis()
    conn = JobConn()
    seen = wire(monkeypatch, **wiring)
    run_id = await analysis_jobs.enqueue_job(r, ticker)
    await analysis_jobs.run_job(r, conn, run_id, ticker, run_date=RUN_DATE)
    return r, conn, seen, run_id


# --- happy path + transitions -----------------------------------------------------

async def test_happy_path_transitions_queued_fetching_scoring_ready(monkeypatch):
    r, conn, seen, run_id = await run_one(monkeypatch, result=engine_result())
    assert r.transitions == [("queued", None), ("running", "fetching"),
                             ("running", "scoring"), ("ready", None)]
    job = await analysis_jobs.get_job(r, run_id)
    assert job["status"] == "ready" and "reason" not in job
    assert job["finished_at"]
    assert seen["override"] == "ok"  # §4a: in-memory gdelt outcome threaded
    assert seen["audit"] == [(RUN_DATE, ok_outcomes())]
    # coalesce pointer cleared; cooldown marker set
    assert "analysis_active:TSLA" not in r.strings
    assert "analysis_last_finished:TSLA" in r.strings


async def test_coverage_promotion_upserts_is_covered_true(monkeypatch):
    _, conn, _, _ = await run_one(monkeypatch, result=engine_result())
    query, args = conn.executed[0]
    assert "INSERT INTO ticker" in query
    assert "ON CONFLICT (full_symbol) DO UPDATE SET is_covered = TRUE" in query
    assert args == ("TSLA", "US", "TSLA")


def test_market_routing_by_suffix_matches_seed_conventions():
    assert analysis_jobs.market_for("2330.TW") == ("2330", "TWSE")
    assert analysis_jobs.market_for("6488.TWO") == ("6488", "TPEx")
    assert analysis_jobs.market_for("AAPL") == ("AAPL", "US")


# --- partial / failed terminal mapping ------------------------------------------------

async def test_one_lens_unavailable_is_partial(monkeypatch):
    r, _, _, run_id = await run_one(
        monkeypatch, result=engine_result(unavailable=("chip",)))
    job = await analysis_jobs.get_job(r, run_id)
    assert job["status"] == "partial" and "reason" not in job


async def test_suppressed_result_is_failed(monkeypatch):
    r, _, _, run_id = await run_one(
        monkeypatch, result=engine_result(unavailable=("chip", "news")),
        outcomes=ok_outcomes(gdelt=("unavailable", "[live] boom")))
    job = await analysis_jobs.get_job(r, run_id)
    assert job["status"] == "failed"
    assert job["reason"] == "source_unavailable"


async def test_gdelt_unavailable_threads_unavailable_override(monkeypatch):
    _, _, seen, _ = await run_one(
        monkeypatch, result=engine_result(unavailable=("news",)),
        outcomes=ok_outcomes(gdelt=("unavailable", "[live] all tickers failed")))
    assert seen["override"] == "unavailable"


async def test_adapter_unavailable_is_failed_source_unavailable(monkeypatch):
    r, _, _, run_id = await run_one(
        monkeypatch, fetch_exc=AdapterUnavailable(HTML_ERROR))
    job = await analysis_jobs.get_job(r, run_id)
    assert job["status"] == "failed"
    assert job["reason"] == "source_unavailable"


async def test_timeout_is_failed_timeout(monkeypatch):
    r, _, _, run_id = await run_one(
        monkeypatch, fetch_exc=TimeoutError("TLS handshake timed out"))
    job = await analysis_jobs.get_job(r, run_id)
    assert job["status"] == "failed" and job["reason"] == "timeout"


async def test_generic_exception_is_failed_fetch_failed(monkeypatch):
    r, _, _, run_id = await run_one(
        monkeypatch, fetch_exc=RuntimeError(HTML_ERROR))
    job = await analysis_jobs.get_job(r, run_id)
    assert job["status"] == "failed" and job["reason"] == "fetch_failed"


async def test_scoring_exception_is_failed_and_isolated(monkeypatch):
    r, _, _, run_id = await run_one(
        monkeypatch, score_exc=RuntimeError(HTML_ERROR))
    job = await analysis_jobs.get_job(r, run_id)
    assert job["status"] == "failed" and job["reason"] == "fetch_failed"


async def test_sec_ondemand_error_hygiene_no_upstream_text_in_job(monkeypatch):
    """SEC-ONDEMAND-ERROR-HYGIENE: an injected upstream HTML error body never
    appears in ANY job field — the reason is a category, full stop."""
    for exc in (RuntimeError(HTML_ERROR), AdapterUnavailable(HTML_ERROR),
                TimeoutError(HTML_ERROR)):
        r, _, _, run_id = await run_one(monkeypatch, fetch_exc=exc)
        job = await analysis_jobs.get_job(r, run_id)
        assert job["reason"] in ("source_unavailable", "fetch_failed", "timeout")
        blob = json.dumps(job)
        assert "html" not in blob.lower() and "abc123" not in blob


async def test_reason_category_when_engine_has_nothing_scoreable(monkeypatch):
    # No exception anywhere, all sources fetched fine, engine writes no row:
    # honest failed with the missing-source category.
    r, _, _, run_id = await run_one(monkeypatch, result=None)
    job = await analysis_jobs.get_job(r, run_id)
    assert job["status"] == "failed" and job["reason"] == "source_unavailable"

    r2, _, _, run_id2 = await run_one(
        monkeypatch, result=None,
        outcomes=ok_outcomes(yfinance=("error", "timeout: fetch exceeded 15s")))
    job2 = await analysis_jobs.get_job(r2, run_id2)
    assert job2["reason"] == "timeout"


# --- worker loop survival ---------------------------------------------------------------

async def test_worker_loop_survives_a_job_crash_and_processes_the_next(monkeypatch):
    r = FakeRedis()
    calls: list[str] = []

    async def fake_get_redis():
        return r

    class _Acquire:
        async def __aenter__(self):
            return JobConn()

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_get_pool():
        return _Pool()

    async def flaky_run_job(r_, conn, run_id, ticker, **kw):
        calls.append(run_id)
        if len(calls) == 1:
            raise RuntimeError(HTML_ERROR)  # escapes run_job's own net
        await analysis_jobs._finish(r_, run_id, ticker, "ready")

    monkeypatch.setattr(analysis_jobs, "get_redis", fake_get_redis)
    monkeypatch.setattr(analysis_jobs, "get_pool", fake_get_pool)
    monkeypatch.setattr(analysis_jobs, "run_job", flaky_run_job)
    monkeypatch.setattr(analysis_jobs, "_IDLE_POLL_S", 0)

    first = await analysis_jobs.enqueue_job(r, "AAA")
    second = await analysis_jobs.enqueue_job(r, "BBB")
    task = asyncio.create_task(analysis_jobs.worker_loop())
    for _ in range(500):
        await asyncio.sleep(0)
        done = await analysis_jobs.get_job(r, second)
        if len(calls) == 2 and done["status"] == "ready":
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    crashed = await analysis_jobs.get_job(r, first)
    assert crashed["status"] == "failed"
    assert crashed["reason"] == "fetch_failed"  # generic category, loop alive
    assert "html" not in json.dumps(crashed).lower()
    survived = await analysis_jobs.get_job(r, second)
    assert survived["status"] == "ready"


# --- score_ticker threads the override (the real function, not the wiring) --------------

async def test_score_ticker_threads_news_override_to_news_signal(monkeypatch):
    import app.services.recommendation_engine as eng

    seen = {}

    async def fake_technical(conn, tid, rec_date):
        return eng.ModuleSignal(signal=None, status="unavailable"), None

    async def fake_fundamental(conn, tid):
        return eng.ModuleSignal(signal=None, status="unavailable"), None, None

    async def fake_chip(conn, tid, exchange):
        return eng.ModuleSignal(signal=None, status="unavailable")

    async def fake_news(conn, tid, asof, symbol, news_fetch_override=None):
        seen["override"] = news_fetch_override
        return eng.ModuleSignal(signal=None, status="unavailable")

    monkeypatch.setattr(eng, "technical_signal", fake_technical)
    monkeypatch.setattr(eng, "fundamental_signal", fake_fundamental)
    monkeypatch.setattr(eng, "chip_signal", fake_chip)
    monkeypatch.setattr(eng, "news_signal", fake_news)

    ticker = {"id": 1, "full_symbol": "AAPL", "exchange": "US"}
    result = await eng.score_ticker(None, ticker, RUN_DATE, {}, 6, "mvp-1.0",
                                    news_fetch_override="ok")
    assert result is None  # nothing scoreable -> no row (unchanged semantics)
    assert seen["override"] == "ok"


# --- run_kind='on_demand' audit rows (migration 0003) ------------------------------------

async def test_on_demand_audit_upsert_uses_the_widened_key():
    conn = JobConn()
    await write_on_demand_audit(conn, RUN_DATE, {
        "yfinance": ("ok", "[fixture] stats"),
        "gdelt": ("unavailable", "[live] all tickers failed"),
    })
    assert len(conn.executed) == 2
    for query, args in conn.executed:
        assert "INSERT INTO pipeline_run" in query
        assert "'on_demand'" in query  # never the daily 'scheduled' rows
        # the 3-col arbiter: on-demand rows can never collide with scheduled
        assert "ON CONFLICT (run_date, source_name, run_kind)" in query
        assert args[0] == RUN_DATE
    assert conn.executed[1][1][2] == "unavailable"


def test_migration_0003_and_schema_mirror_agree_on_run_kind():
    schema = (REPO_ROOT / "db" / "schema.sql").read_text()
    assert "run_kind    TEXT        NOT NULL DEFAULT 'scheduled'" in schema
    assert "UNIQUE (run_date, source_name, run_kind)" in schema
    assert "UNIQUE (run_date, source_name)\n" not in schema  # old key replaced

    migration = (REPO_ROOT / "db" / "migrations"
                 / "0003_on_demand_v1210.sql").read_text()
    assert ("ADD COLUMN IF NOT EXISTS run_kind TEXT NOT NULL DEFAULT 'scheduled'"
            in migration)
    assert "DROP CONSTRAINT IF EXISTS pipeline_run_run_date_source_name_key" in migration
    assert "UNIQUE (run_date, source_name, run_kind)" in migration


def test_daily_pipeline_write_matches_the_widened_arbiter():
    # The 2-col arbiter would no longer match any unique constraint after
    # 0003 — the daily writes must name the widened key + run_kind explicitly.
    source = (REPO_ROOT / "app" / "batch" / "pipeline.py").read_text()
    assert "ON CONFLICT (run_date, source_name)\n" not in source
    assert source.count("ON CONFLICT (run_date, source_name, run_kind)") == 2
    assert "'running', now(), 'scheduled'" in source


# --- A8 named gate hooks (co-gated by A6/A8 at the #20 candidate) ------------------
# SEC-ONDEMAND-FAILCLOSED: T12-M1-FAILCLOSED carried to on-demand — a failed
# on-demand news fetch yielding 0 items must fail the lens CLOSED (unavailable),
# never land on a masked neutral 0.0.
# SEC-ONDEMAND-PROVENANCE-ISOLATION: run_kind seals both directions — the
# daily read never picks up an on-demand row, and the override keeps the
# on-demand path off the daily row entirely.

async def test_sec_ondemand_failclosed_news_fault_never_neutral():
    from datetime import date

    from app.batch.signals.news import news_signal

    class _Conn:
        async def fetchrow(self, q, *a):
            raise AssertionError("override path must not read pipeline_run")

        async def fetch(self, q, *a):
            return []  # 0 news_item rows — the ambiguous state

    sig = await news_signal(_Conn(), 1, date(2026, 7, 9), "AAPL",
                            news_fetch_override="unavailable")
    assert sig.status == "unavailable" and sig.signal is None  # never ok/0.0


async def test_sec_ondemand_provenance_isolation_both_directions():
    from datetime import date

    from app.batch.signals.news import news_signal

    class _Conn:
        """Only an on-demand gdelt row exists for the date; the daily read
        must NOT accept it as fetch evidence."""

        def __init__(self):
            self.queries = []

        async def fetchrow(self, q, *a):
            self.queries.append(q)
            assert "run_kind = 'scheduled'" in q  # read-side pin (A3/A8 c)
            return None  # no scheduled row -> daily path reads unavailable

        async def fetch(self, q, *a):
            return [{"sentiment": None}]

    conn = _Conn()
    # direction 1: daily path with only an on-demand row present -> unavailable
    daily = await news_signal(conn, 1, date(2026, 7, 9), "AAPL")
    assert daily.status == "unavailable" and conn.queries
    # direction 2: on-demand path never consults pipeline_run at all
    class _NoRunConn(_Conn):
        async def fetchrow(self, q, *a):
            raise AssertionError("on-demand override must not read pipeline_run")

        async def fetch(self, q, *a):
            return []

    ondemand = await news_signal(_NoRunConn(), 1, date(2026, 7, 9), "AAPL",
                                 news_fetch_override="ok")
    assert ondemand.status == "ok" and str(ondemand.signal) == "0.00"
