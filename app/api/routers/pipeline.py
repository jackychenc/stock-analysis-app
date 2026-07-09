from fastapi import APIRouter, Depends

from app.api.deps import current_user
from app.batch.pipeline import KNOWN_SOURCES, run_pipeline_once
from app.db.pool import get_pool
from app.schemas.contracts import PipelineSource, PipelineStatus

router = APIRouter(prefix="/pipeline", tags=["ops"], dependencies=[Depends(current_user)])


@router.get("/status", response_model=PipelineStatus)
async def pipeline_status() -> PipelineStatus:
    pool = await get_pool()
    async with pool.acquire() as conn:
        # run_kind='scheduled' everywhere (task #20): /pipeline/status reports
        # the NIGHTLY batch (NFR-01 SLA, R-01 alerting) — on-demand audit rows
        # are per-ticker probes and must never masquerade as a daily outcome.
        latest = await conn.fetchrow(
            "SELECT max(run_date) AS run_date FROM pipeline_run"
            " WHERE run_kind = 'scheduled'"
        )
        run_date = latest["run_date"] if latest else None
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (source_name) source_name, run_date, status, finished_at,
                (SELECT count(*) FROM (
                    SELECT run_date FROM pipeline_run p2
                    WHERE p2.source_name = p1.source_name
                      AND p2.run_kind = 'scheduled'
                      AND p2.status IN ('unavailable', 'error')
                      AND p2.run_date > COALESCE(
                          (SELECT max(p3.run_date) FROM pipeline_run p3
                           WHERE p3.source_name = p1.source_name
                             AND p3.run_kind = 'scheduled' AND p3.status = 'ok'),
                          '1970-01-01')
                ) bad) AS consecutive_bad_days
            FROM pipeline_run p1
            WHERE p1.run_kind = 'scheduled'
            ORDER BY source_name, run_date DESC
            """
        )
    by_source = {r["source_name"]: r for r in rows}
    completed = max(
        (r["finished_at"] for r in rows if r["finished_at"] is not None), default=None
    )
    sources = [
        PipelineSource(
            source_name=name,
            status=by_source[name]["status"] if name in by_source else "never_run",
            consecutive_bad_days=(
                by_source[name]["consecutive_bad_days"] if name in by_source else 0
            ),
        )
        for name in KNOWN_SOURCES
    ]
    sla_met = None
    if run_date is not None and completed is not None:
        # NFR-01: snapshot ready by 07:00 Taiwan time (UTC+8) on run_date.
        from datetime import datetime, time, timedelta, timezone

        deadline = datetime.combine(
            run_date, time(7, 0), tzinfo=timezone(timedelta(hours=8))
        )
        sla_met = completed <= deadline
    return PipelineStatus(
        run_date=run_date, completed_at=completed, sla_met=sla_met, sources=sources
    )


@router.post("/run-now")
async def run_now() -> dict[str, str]:
    """Local-test trigger (repo-layout.md): runs the batch skeleton on demand."""
    await run_pipeline_once()
    return {"status": "completed"}
