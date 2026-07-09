"""On-demand ticker analysis routes — task #20 (ADR-009 v1.2.10, FR-61/62,
US-14). POST enqueues (or short-circuits to the snapshot); GET polls the job.
The job store/queue is Redis (app.db.redis); execution reuses the batch via
app.services.analysis_jobs."""

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import current_user
from app.batch.adapters.common import SYMBOL_RE
from app.core.config import get_settings
from app.db.pool import get_pool
from app.db.redis import get_redis
from app.services import analysis_jobs, read_service

router = APIRouter(prefix="/analyze", tags=["analysis"],
                   dependencies=[Depends(current_user)])


class AnalyzeRequest(BaseModel):
    ticker: str
    force: bool = False


def _ready(ticker: str, rec_date: date | None,
           next_refresh_at: str | None = None) -> dict:
    """200 body: the snapshot answers — stale means the caller may `force`."""
    today = datetime.now(UTC).date()
    return {"status": "ready", "ticker": ticker,
            "stale": rec_date is None or rec_date < today,
            "as_of": rec_date,
            "next_refresh_at": next_refresh_at}


def _accepted(run_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"run_id": run_id, "status": "queued",
                 "poll_after_ms": get_settings().analyze_poll_after_ms},
    )


@router.post("")
async def analyze(body: AnalyzeRequest):
    """US-14: analyze any ticker on demand.
    - LAYER 1 ingress validation: SYMBOL_RE fullmatch — the same regex that
      guards adapter egress (layer 2, untouched there) — so a malformed
      symbol is a clean 400 VALIDATION_ERROR, never a 500/stacktrace.
    - Covered + snapshot exists + not force -> 200 ready (stale flagged).
    - COALESCE: a queued/running job for the ticker returns ITS run_id —
      never two concurrent runs per ticker.
    - COOLDOWN (on_demand_cooldown_s): force bypasses the fresh short-circuit
      but honors the cooldown — a just-finished ticker is served as ready.
    - POOL CAP (FR-61, max_coverage_pool_size): a NEW ticker that would push
      the covered pool past the cap (benchmarks are is_covered=false and
      never count) is a surfaced 409, never a silent drop.
    """
    if not SYMBOL_RE.fullmatch(body.ticker):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "VALIDATION_ERROR",
                    "message": "Ticker has an invalid format."},
        )
    ticker = body.ticker.upper()  # full_symbol convention (seed/adapters)
    settings = get_settings()
    r = await get_redis()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await read_service.fetch_ticker(conn, ticker)
        rec_date = None
        if row is not None:
            rec_date = await conn.fetchval(
                "SELECT max(rec_date) FROM recommendation WHERE ticker_id = $1",
                row["id"],
            )
        covered = row is not None and row["is_covered"]
        if covered and rec_date is not None and not body.force:
            return _ready(ticker, rec_date,
                          await analysis_jobs.next_refresh_at(r, ticker))  # fresh short-circuit

        existing = await analysis_jobs.active_run_id(r, ticker)
        if existing is not None:
            return _accepted(existing)  # coalesce onto the in-flight run

        since = await analysis_jobs.seconds_since_last_finish(r, ticker)
        if since is not None and since < settings.on_demand_cooldown_s:
            return _ready(ticker, rec_date,
                          await analysis_jobs.next_refresh_at(r, ticker))  # cooldown

        if not covered:
            pool_size = await conn.fetchval(
                "SELECT count(*) FROM ticker WHERE is_covered"
            )
            if pool_size >= settings.max_coverage_pool_size:
                # FR-61: surfaced, never silent — dedicated code (A3 enum).
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "COVERAGE_POOL_FULL",
                        # A1/A4 condition: actionable, never a dead-end —
                        # name the cap AND the current remedy (config path;
                        # #14 adds the manage-pool UI link).
                        "message": (
                            f"Coverage pool full ({pool_size} of "
                            f"{settings.max_coverage_pool_size}). To add "
                            f"{ticker}: remove a tracked ticker, or raise "
                            f"MAX_COVERAGE_POOL_SIZE in .env."
                        ),
                    },
                )

        run_id = await analysis_jobs.enqueue_job(r, ticker)
    return _accepted(run_id)


@router.get("/{run_id}")
async def analyze_status(run_id: str) -> dict:
    """Poll one job. phase only while running; reason only when failed, and
    always a sanitized category (source_unavailable|fetch_failed|timeout) —
    never upstream bodies/stacktraces/PII (SEC-ONDEMAND-ERROR-HYGIENE)."""
    r = await get_redis()
    job = await analysis_jobs.get_job(r, run_id)
    if job is None:
        # ApiError codes are frozen (contract v1.0) — VALIDATION_ERROR is the
        # closest fit for an unknown run_id; the 404 carries the semantics.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "VALIDATION_ERROR", "message": "Unknown run_id."},
        )
    out = {"run_id": run_id, "ticker": job.get("ticker"),
           "status": job.get("status")}
    if out["status"] == "running" and job.get("phase"):
        out["phase"] = job["phase"]
    if out["status"] == "failed" and job.get("reason"):
        out["reason"] = job["reason"]
    return out
