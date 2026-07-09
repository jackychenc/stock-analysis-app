"""On-demand ticker analysis jobs — task #20 (ADR-009 v1.2.10, FR-61/62).

Store + queue live in Redis (loopback-bound already — NFR-22):
- job hash   analysis_job:{run_id}   fields: ticker, status, phase, reason,
  created_at, finished_at. status ∈ queued|running|ready|partial|failed;
  phase ∈ fetching|scoring while running; reason is a SANITIZED CATEGORY ONLY
  (source_unavailable|fetch_failed|timeout) — upstream bodies/stacktraces stay
  in logs and audit rows, never in the job (SEC-ONDEMAND-ERROR-HYGIENE);
- queue      analysis_queue          payloads carry ticker+run_id ONLY;
- pointer    analysis_active:{TICKER} -> run_id while queued/running — the
  COALESCE guard (never two concurrent runs per ticker);
- marker     analysis_last_finished:{TICKER} -> epoch seconds — the COOLDOWN
  guard (compared against on_demand_cooldown_s at request time, so a config
  change applies immediately).

Execution REUSES the batch, no fork: pipeline.run_on_demand_fetch runs the
four ingest adapters for just this ticker (same client selection as the daily
stages), outcomes land as run_kind='on_demand' pipeline_run audit rows, and
the engine's score_ticker runs with the in-memory gdelt fetch outcome (the
§4a seam in signals/news.py). The worker is an in-process asyncio task
(FastAPI lifespan) consuming one job at a time — single-user scale.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.batch.adapters.common import AdapterUnavailable
from app.batch.pipeline import run_on_demand_fetch, write_on_demand_audit
from app.core.config import get_settings
from app.db.pool import get_pool
from app.db.redis import get_redis
from app.services.recommendation_engine import _load_weights, score_ticker

logger = logging.getLogger(__name__)

QUEUE_KEY = "analysis_queue"
_IDLE_POLL_S = 1  # bounded blpop wait so cancellation (shutdown) stays prompt


def job_key(run_id: str) -> str:
    return f"analysis_job:{run_id}"


def active_key(ticker: str) -> str:
    return f"analysis_active:{ticker}"


def last_finished_key(ticker: str) -> str:
    return f"analysis_last_finished:{ticker}"


def market_for(full_symbol: str) -> tuple[str, str]:
    """(symbol, exchange) routed by suffix — the scripts/seed.py convention:
    .TW -> TWSE bare code, .TWO -> TPEx bare code, anything else US."""
    if full_symbol.endswith(".TW"):
        return full_symbol.removesuffix(".TW"), "TWSE"
    if full_symbol.endswith(".TWO"):
        return full_symbol.removesuffix(".TWO"), "TPEx"
    return full_symbol, "US"


# --- job store ----------------------------------------------------------------

async def enqueue_job(r: Any, ticker: str) -> str:
    """Create + queue a job. run_id is uuid4 hex minted at request time (app
    code, not a workflow — fine at runtime). The queue payload carries
    ticker+run_id ONLY — no secrets ever transit Redis."""
    run_id = uuid.uuid4().hex
    await r.hset(job_key(run_id), mapping={
        "ticker": ticker, "status": "queued",
        "created_at": datetime.now(UTC).isoformat(),
    })
    await r.set(active_key(ticker), run_id)
    await r.rpush(QUEUE_KEY, json.dumps({"run_id": run_id, "ticker": ticker}))
    return run_id


async def active_run_id(r: Any, ticker: str) -> str | None:
    """COALESCE guard: the queued/running job's run_id for this ticker, if
    one exists — POST /analyze returns it instead of minting a second run."""
    run_id = await r.get(active_key(ticker))
    if not run_id:
        return None
    status = await r.hget(job_key(run_id), "status")
    return run_id if status in ("queued", "running") else None


async def seconds_since_last_finish(r: Any, ticker: str) -> float | None:
    """COOLDOWN input: seconds since this ticker's last job reached a terminal
    status; None when it never has."""
    raw = await r.get(last_finished_key(ticker))
    return None if raw is None else time.time() - float(raw)


async def next_refresh_at(r: Any, ticker: str) -> str | None:
    """ADR-009 (A3 ruling): server-authoritative Refresh availability.
    Absolute ISO timestamp (last terminal run + cooldown window) while the
    ticker is inside its cooldown; None once Refresh is available. Absolute
    beats a relative remaining-ms (stale by transit); server state beats a
    client-duplicated constant (drift = silent no-op dead-end)."""
    from app.core.config import get_settings

    since = await seconds_since_last_finish(r, ticker)
    cooldown = get_settings().on_demand_cooldown_s
    if since is None or since >= cooldown:
        return None
    return (datetime.now(UTC) + timedelta(seconds=cooldown - since)).isoformat()


async def get_job(r: Any, run_id: str) -> dict[str, str] | None:
    fields = await r.hgetall(job_key(run_id))
    return fields or None


async def _mark(r: Any, run_id: str, **fields: str) -> None:
    await r.hset(job_key(run_id), mapping=fields)


async def _finish(r: Any, run_id: str, ticker: str, status: str,
                  reason: str | None = None) -> None:
    """Terminal transition: job hash, coalesce pointer, cooldown marker."""
    fields = {"status": status, "finished_at": datetime.now(UTC).isoformat()}
    if reason is not None:
        fields["reason"] = reason
    await r.hset(job_key(run_id), mapping=fields)
    await r.delete(active_key(ticker))
    await r.set(last_finished_key(ticker), str(time.time()))


# --- job execution --------------------------------------------------------------

# Coverage-pool promotion (FR-61): the on-demand ticker joins the covered
# universe — the nightly batch then maintains it like any other ticker.
_UPSERT_COVERED_TICKER = """
    INSERT INTO ticker (symbol, exchange, full_symbol, is_covered)
    VALUES ($1, $2, $3, TRUE)
    ON CONFLICT (full_symbol) DO UPDATE SET is_covered = TRUE
"""


async def _ensure_covered_ticker(conn: Any, full_symbol: str) -> Any:
    symbol, exchange = market_for(full_symbol)
    await conn.execute(_UPSERT_COVERED_TICKER, symbol, exchange, full_symbol)
    return await conn.fetchrow(
        "SELECT id, full_symbol, exchange FROM ticker WHERE full_symbol = $1",
        full_symbol,
    )


def _reason_from_outcomes(outcomes: dict[str, tuple[str, str]]) -> str:
    """Terminal category when the engine produced no usable composite and no
    exception escaped the per-source isolation. Categories only — the outcome
    messages stay in the audit rows, never in the job."""
    statuses = [status for status, _ in outcomes.values()]
    messages = [message for _, message in outcomes.values()]
    if any(m.startswith("timeout") for m in messages):
        return "timeout"
    if any(s == "unavailable" for s in statuses):
        return "source_unavailable"
    if any(s == "error" for s in statuses):
        return "fetch_failed"
    return "source_unavailable"  # fetched fine, still nothing scoreable


async def run_job(r: Any, conn: Any, run_id: str, ticker: str, *,
                  run_date: date | None = None) -> None:
    """One on-demand job: promote coverage -> phase=fetching (4 adapters,
    this ticker only, audit rows) -> phase=scoring (engine core with the
    in-memory news outcome) -> terminal status. Every failure maps to a
    sanitized reason category; the traceback goes to logs only."""
    run_date = run_date or datetime.now(UTC).date()
    try:
        await _mark(r, run_id, status="running", phase="fetching")
        ticker_row = await _ensure_covered_ticker(conn, ticker)
        outcomes = await run_on_demand_fetch(conn, ticker)
        await write_on_demand_audit(conn, run_date, outcomes)
        # §4a seam input: a single-ticker gdelt run that failed entirely reads
        # 'unavailable' (ingest_gdelt raises when its only ticker fails), so
        # the source status alone is the discriminator here.
        gdelt_status = outcomes.get("gdelt", ("unavailable", ""))[0]
        override = "ok" if gdelt_status == "ok" else "unavailable"
        await _mark(r, run_id, status="running", phase="scoring")
        weights, horizon_months = await _load_weights(conn)
        result = await score_ticker(
            conn, ticker_row, run_date, weights, horizon_months,
            get_settings().methodology_version, news_fetch_override=override,
        )
    except AdapterUnavailable:
        logger.exception("on-demand job %s: source unavailable", run_id)
        await _finish(r, run_id, ticker, "failed", reason="source_unavailable")
        return
    except TimeoutError:
        logger.exception("on-demand job %s: timeout", run_id)
        await _finish(r, run_id, ticker, "failed", reason="timeout")
        return
    except Exception:
        # A8 #6 log hygiene: exception detail (possibly upstream bodies) goes
        # to the log; the job carries the category ONLY.
        logger.exception("on-demand job %s crashed", run_id)
        await _finish(r, run_id, ticker, "failed", reason="fetch_failed")
        return

    if result is None or result.composite_call == "SUPPRESSED":
        # No computable composite (or FR-35 suppression): an honest failure.
        await _finish(r, run_id, ticker, "failed",
                      reason=_reason_from_outcomes(outcomes))
    elif result.missing_modules:
        await _finish(r, run_id, ticker, "partial")
    else:
        await _finish(r, run_id, ticker, "ready")


async def worker_loop() -> None:
    """In-process queue consumer (FastAPI lifespan task): one job at a time —
    single-user scale (ADR-009). The loop survives ANY job/infra exception
    (run_job already fails jobs closed; this net catches queue/pool crashes);
    only cancellation (shutdown) exits it."""
    logger.info("on-demand analysis worker started")
    while True:
        payload = None
        try:
            r = await get_redis()
            item = await r.blpop(QUEUE_KEY, timeout=_IDLE_POLL_S)
            if item is None:
                continue
            payload = json.loads(item[1])
            pool = await get_pool()
            async with pool.acquire() as conn:
                await run_job(r, conn, payload["run_id"], payload["ticker"])
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never a dead worker; never upstream text in the job hash.
            logger.exception("analysis worker iteration failed")
            if payload is not None:
                try:
                    await _finish(await get_redis(), payload["run_id"],
                                  payload["ticker"], "failed",
                                  reason="fetch_failed")
                except Exception:
                    logger.exception("analysis worker could not mark job failed")
            await asyncio.sleep(_IDLE_POLL_S)
