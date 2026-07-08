"""Batch pipeline orchestrator (deck §22.1: ingest → signal → score → backtest
→ persist) with per-source isolation (§22.4: one adapter failure never aborts
peers).

Task #8: yfinance (price_bar + fundamental) is LIVE. Chip (task #9), news
(task #12) remain honest 'unavailable' stubs; signal calculators + scoring are
task #10. `pipeline_run` rows record ok/unavailable/error per source, feeding
/pipeline/status and the R-01 consecutive-bad-days alert.
"""

import logging
from datetime import UTC, date, datetime

from app.batch.adapters.yfinance_adapter import (
    AdapterUnavailable,
    FixtureYFinanceClient,
    RealYFinanceClient,
    ingest_yfinance,
)
from app.core.config import get_settings
from app.db.pool import get_pool

logger = logging.getLogger(__name__)

KNOWN_SOURCES = ("yfinance", "twse_tpex", "edgar_13f", "gdelt")

_NOT_IMPLEMENTED = {
    "twse_tpex": "adapter lands in roadmap Step 3 (task #9)",
    "edgar_13f": "adapter lands in roadmap Step 3 (task #9)",
    "gdelt": "adapter lands in roadmap Step 6 (task #12)",
}


async def _run_yfinance(conn) -> tuple[str, str]:
    """Returns (status, message). Fixture mode (FR-19) is env-switchable so CI
    and stack smokes never hit Yahoo (R-01 ToS-gray/rate-limited)."""
    settings = get_settings()
    client = FixtureYFinanceClient() if settings.yfinance_fixture_mode else RealYFinanceClient()
    mode = "fixture" if settings.yfinance_fixture_mode else "live"
    try:
        stats = await ingest_yfinance(conn, client)
    except AdapterUnavailable as exc:
        return "unavailable", f"[{mode}] {exc}"
    if stats.tickers_failed:
        # Partial success: source is up, but failures are named — never silent.
        return "ok", f"[{mode}] partial: {stats.summary()}"
    return "ok", f"[{mode}] {stats.summary()}"


async def run_pipeline_once(run_date: date | None = None) -> None:
    run_date = run_date or datetime.now(UTC).date()
    pool = await get_pool()
    async with pool.acquire() as conn:
        for source in KNOWN_SOURCES:
            # Per-source isolation: each source is its own try/except so a
            # failure never aborts peers (deck §22.4).
            await conn.execute(
                """
                INSERT INTO pipeline_run (run_date, source_name, status, started_at)
                VALUES ($1, $2, 'running', now())
                ON CONFLICT (run_date, source_name)
                DO UPDATE SET status = 'running', started_at = now(),
                              finished_at = NULL, message = NULL
                """,
                run_date, source,
            )
            try:
                if source == "yfinance":
                    status, message = await _run_yfinance(conn)
                else:
                    status, message = "unavailable", _NOT_IMPLEMENTED[source]
            except Exception as exc:
                # A8 #6 log hygiene: generic status, no response bodies.
                status, message = "error", f"unexpected: {exc}"
                logger.exception("pipeline source=%s crashed", source)
            await conn.execute(
                """
                UPDATE pipeline_run
                SET status = $3, finished_at = now(), message = $4
                WHERE run_date = $1 AND source_name = $2
                """,
                run_date, source, status, message,
            )
            logger.info("pipeline source=%s status=%s", source, status)
