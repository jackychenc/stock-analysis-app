"""Batch pipeline orchestrator — Foundation SKELETON.

Structure per deck §22.1 (ingest → signal → score → backtest → persist) with
per-source isolation (§22.4: one adapter failure never aborts peers). Real
adapters land in tasks #8/#9/#12; the engine in #10; backtest in #13.
The skeleton records honest 'unavailable' pipeline_run rows so /pipeline/status
and the R-01 bad-day counter are exercisable from day one.
"""

import logging
from datetime import UTC, date, datetime

from app.db.pool import get_pool

logger = logging.getLogger(__name__)

KNOWN_SOURCES = ("yfinance", "twse_tpex", "edgar_13f", "gdelt")

_NOT_IMPLEMENTED = {
    "yfinance": "adapter lands in roadmap Step 2 (task #8)",
    "twse_tpex": "adapter lands in roadmap Step 3 (task #9)",
    "edgar_13f": "adapter lands in roadmap Step 3 (task #9)",
    "gdelt": "adapter lands in roadmap Step 6 (task #12)",
}


async def run_pipeline_once(run_date: date | None = None) -> None:
    run_date = run_date or datetime.now(UTC).date()
    pool = await get_pool()
    async with pool.acquire() as conn:
        for source in KNOWN_SOURCES:
            # Per-source isolation: each source is its own try/except so a
            # failure never aborts peers (deck §22.4).
            try:
                message = _NOT_IMPLEMENTED[source]
                status = "unavailable"
            except Exception as exc:  # pragma: no cover - skeleton
                message, status = str(exc), "error"
            await conn.execute(
                """
                INSERT INTO pipeline_run (run_date, source_name, status, started_at,
                                          finished_at, message)
                VALUES ($1, $2, $3, now(), now(), $4)
                ON CONFLICT (run_date, source_name)
                DO UPDATE SET status = EXCLUDED.status, finished_at = now(),
                              message = EXCLUDED.message
                """,
                run_date, source, status, message,
            )
            logger.info("pipeline source=%s status=%s", source, status)
