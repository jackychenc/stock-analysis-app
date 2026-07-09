"""Task #20 route contract (ADR-009 v1.2.10 / FR-61/62 / US-14) — POST
/api/v1/analyze + GET /api/v1/analyze/{run_id}: layer-1 ingress validation,
fresh short-circuit, coalesce, cooldown, FR-61 pool cap, job polling, and the
FR-39 disclaimer non-regression. Redis is the conftest FakeRedis — no live
services anywhere."""

import json
import time
from datetime import date

from app.core.config import get_settings
from tests.conftest import make_recommendation

ANALYZE = "/api/v1/analyze"
QUEUE = "analysis_queue"


def queued_payloads(fake_redis) -> list[dict]:
    return [json.loads(item) for item in fake_redis.lists.get(QUEUE, [])]


def fresh_recommendation(ticker_id: int = 1) -> dict:
    rec = make_recommendation(ticker_id=ticker_id)
    rec["rec_date"] = date.today()  # today's snapshot: NOT stale
    return rec


# --- US-14: layer-1 ingress validation ------------------------------------------

def test_invalid_ticker_formats_are_clean_400_and_never_enqueue(web_client, fake_redis):
    for bad in ("<script>", "AAPL\n", "", "AAPL;DROP TABLE", "A" * 13, "TSM C"):
        r = web_client.post(ANALYZE, json={"ticker": bad})
        assert r.status_code == 400, f"expected clean 400 for {bad!r}"
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR"  # never a 500
    assert queued_payloads(fake_redis) == []  # NO job, NO queue entry
    assert fake_redis.hashes == {}


def test_analyze_requires_auth(client):
    assert client.post(ANALYZE, json={"ticker": "AAPL"}).status_code == 401


# --- enqueue / short-circuit -----------------------------------------------------

def test_valid_uncovered_ticker_enqueues_202(web_client, fake_redis):
    r = web_client.post(ANALYZE, json={"ticker": "TSLA"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["poll_after_ms"] == get_settings().analyze_poll_after_ms
    run_id = body["run_id"]
    job = fake_redis.hashes[f"analysis_job:{run_id}"]
    assert job["ticker"] == "TSLA" and job["status"] == "queued"
    # queue payload carries ticker+run_id ONLY — no secrets transit Redis
    assert queued_payloads(fake_redis) == [{"run_id": run_id, "ticker": "TSLA"}]


def test_fresh_covered_snapshot_short_circuits_200(web_client, store, fake_redis):
    store["recommendations"].append(fresh_recommendation())
    r = web_client.post(ANALYZE, json={"ticker": "2330.TW"})
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "ticker": "2330.TW", "stale": False,
                        "as_of": date.today().isoformat(),
                        "next_refresh_at": None}  # ADR-009: null = available
    assert queued_payloads(fake_redis) == []  # served from snapshot, no job


def test_stale_snapshot_is_200_ready_with_stale_flag(web_client, store):
    store["recommendations"].append(make_recommendation(ticker_id=1))  # 2026-07-07
    r = web_client.post(ANALYZE, json={"ticker": "2330.TW"})
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True and body["as_of"] == "2026-07-07"


def test_force_on_stale_snapshot_enqueues(web_client, store, fake_redis):
    store["recommendations"].append(make_recommendation(ticker_id=1))
    r = web_client.post(ANALYZE, json={"ticker": "2330.TW", "force": True})
    assert r.status_code == 202
    assert r.json()["status"] == "queued"
    assert len(queued_payloads(fake_redis)) == 1


# --- coalesce / cooldown -----------------------------------------------------------

def test_coalesce_second_post_returns_the_same_run_id(web_client, fake_redis):
    first = web_client.post(ANALYZE, json={"ticker": "TSLA"})
    second = web_client.post(ANALYZE, json={"ticker": "TSLA"})
    assert second.status_code == 202
    assert second.json()["run_id"] == first.json()["run_id"]
    assert len(queued_payloads(fake_redis)) == 1  # never two concurrent runs


def test_cooldown_blocks_reenqueue_even_with_force(web_client, store, fake_redis):
    # A job for the ticker finished moments ago: force bypasses the fresh
    # short-circuit but HONORS the cooldown — served as ready, no new job.
    store["recommendations"].append(make_recommendation(ticker_id=1))  # stale
    fake_redis.strings["analysis_last_finished:2330.TW"] = str(time.time())
    r = web_client.post(ANALYZE, json={"ticker": "2330.TW", "force": True})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready" and body["stale"] is True
    assert queued_payloads(fake_redis) == []


def test_cooldown_expired_allows_reenqueue(web_client, store, fake_redis):
    store["recommendations"].append(make_recommendation(ticker_id=1))
    expired = time.time() - get_settings().on_demand_cooldown_s - 1
    fake_redis.strings["analysis_last_finished:2330.TW"] = str(expired)
    r = web_client.post(ANALYZE, json={"ticker": "2330.TW", "force": True})
    assert r.status_code == 202


# --- FR-61: coverage-pool cap --------------------------------------------------------

def test_pool_at_cap_blocks_new_ticker_with_surfaced_409(web_client, fake_redis,
                                                         monkeypatch):
    # The store has exactly 2 covered tickers (XXXX.TW is not covered).
    monkeypatch.setenv("MAX_COVERAGE_POOL_SIZE", "2")
    get_settings.cache_clear()
    r = web_client.post(ANALYZE, json={"ticker": "TSLA"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "VALIDATION_ERROR"
    assert "2" in detail["message"]  # the cap is NAMED — surfaced, never silent
    assert queued_payloads(fake_redis) == []


def test_pool_cap_counts_only_covered_tickers(web_client, monkeypatch):
    # 3 ticker rows exist but only 2 are covered (benchmarks/uncovered rows
    # never count toward FR-61) — cap 3 still admits a new ticker.
    monkeypatch.setenv("MAX_COVERAGE_POOL_SIZE", "3")
    get_settings.cache_clear()
    assert web_client.post(ANALYZE, json={"ticker": "TSLA"}).status_code == 202


def test_pool_cap_never_blocks_an_already_covered_ticker(web_client, store,
                                                         monkeypatch):
    monkeypatch.setenv("MAX_COVERAGE_POOL_SIZE", "2")
    get_settings.cache_clear()
    store["recommendations"].append(make_recommendation(ticker_id=1))  # stale
    r = web_client.post(ANALYZE, json={"ticker": "2330.TW", "force": True})
    assert r.status_code == 202  # re-analysis of a pool member is not growth


# --- GET /analyze/{run_id} -------------------------------------------------------------

def test_unknown_run_id_is_404(web_client):
    r = web_client.get(f"{ANALYZE}/{'f' * 32}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_poll_shapes_per_status(web_client, fake_redis):
    run_id = web_client.post(ANALYZE, json={"ticker": "TSLA"}).json()["run_id"]
    key = f"analysis_job:{run_id}"

    r = web_client.get(f"{ANALYZE}/{run_id}")
    assert r.json() == {"run_id": run_id, "ticker": "TSLA", "status": "queued"}

    fake_redis.hashes[key].update({"status": "running", "phase": "fetching"})
    assert web_client.get(f"{ANALYZE}/{run_id}").json()["phase"] == "fetching"

    fake_redis.hashes[key].update({"status": "failed", "reason": "timeout"})
    body = web_client.get(f"{ANALYZE}/{run_id}").json()
    assert body["status"] == "failed" and body["reason"] == "timeout"
    assert "phase" not in body  # phase is only surfaced while running

    fake_redis.hashes[key].update({"status": "ready"})
    body = web_client.get(f"{ANALYZE}/{run_id}").json()
    assert body == {"run_id": run_id, "ticker": "TSLA", "status": "ready"}


# --- FR-39 non-regression ---------------------------------------------------------------

def test_analyze_responses_carry_disclaimer_header(web_client):
    r = web_client.post(ANALYZE, json={"ticker": "TSLA"})
    assert r.headers["X-Disclaimer"] == get_settings().disclaimer_text
