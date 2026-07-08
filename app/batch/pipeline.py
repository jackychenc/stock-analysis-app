"""Batch pipeline orchestrator (deck §22.1: ingest → signal → score → backtest
→ persist) with per-source isolation (§22.4: one adapter failure never aborts
peers).

Task #8: yfinance (price_bar + fundamental) is LIVE. Task #9: chip sources
twse_tpex (chip_data_tw) + edgar_13f (institutional_position_us) are LIVE —
two adapters, two pipeline_run rows, independently isolated (T9-O1). Task
#12: gdelt (news_item) is LIVE. Task #10: the ENGINE stage
(signal calculators + recommendation writes, domain-contract §1–§10) runs
AFTER the ingestion sources and records its own pipeline_run row under
source_name='engine' — schema.sql constrains pipeline_run only by
UNIQUE(run_date, source_name) (no CHECK on source_name values), so the engine
row is legal; /pipeline/status keeps reporting the 4 ingestion sources.
`pipeline_run` rows record ok/unavailable/error per source, feeding
/pipeline/status and the R-01 consecutive-bad-days alert.
"""

import asyncio
import logging
from datetime import UTC, date, datetime

from app.batch.adapters.common import AdapterUnavailable
from app.batch.adapters.edgar_adapter import (
    FixtureEdgarClient,
    RealEdgarClient,
    ingest_edgar_13f,
    load_curated_13f,
)
from app.batch.adapters.gdelt_adapter import (
    FixtureGdeltClient,
    RealGdeltClient,
    ingest_gdelt,
    load_news_queries,
)
from app.batch.adapters.twse_tpex_adapter import (
    FixtureTwseTpexClient,
    RealTwseTpexClient,
    ingest_twse_tpex,
)
from app.batch.adapters.yfinance_adapter import (
    FixtureYFinanceClient,
    RealYFinanceClient,
    ingest_yfinance,
)
from app.core.config import get_settings
from app.db.pool import get_pool
from app.services.recommendation_engine import run_engine

logger = logging.getLogger(__name__)

KNOWN_SOURCES = ("yfinance", "twse_tpex", "edgar_13f", "gdelt")
# Task #10: derived stage, not an ingestion source — tracked in pipeline_run
# for ops visibility but deliberately NOT in KNOWN_SOURCES (the /pipeline
# status contract lists the 4 external sources).
ENGINE_SOURCE = "engine"


async def _no_pacing(_delay: float) -> None:
    """Fixture-mode sleeper: nothing to pace — no live egress (FR-19/R-01)."""


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


async def _run_twse_tpex(conn) -> tuple[str, str]:
    """TW chip facts -> chip_data_tw. Fixture mode (FR-19/T9-D1) keeps CI off
    the live TWSE/TPEx OpenAPI (R-01)."""
    settings = get_settings()
    fixture = settings.twse_tpex_fixture_mode
    client = FixtureTwseTpexClient() if fixture else RealTwseTpexClient()
    mode = "fixture" if fixture else "live"
    try:
        stats = await ingest_twse_tpex(conn, client)
    except AdapterUnavailable as exc:
        return "unavailable", f"[{mode}] {exc}"
    if stats.tickers_failed:
        return "ok", f"[{mode}] partial: {stats.summary()}"
    return "ok", f"[{mode}] {stats.summary()}"


async def _run_edgar_13f(conn) -> tuple[str, str]:
    """US quarterly positioning (13F, delayed — R-04/FR-16) ->
    institutional_position_us. Curated filers from config (PM condition);
    fixture mode keeps CI off live EDGAR."""
    settings = get_settings()
    curated = load_curated_13f(settings.curated_13f_path)
    fixture = settings.edgar_fixture_mode
    client = FixtureEdgarClient(curated) if fixture else RealEdgarClient()
    mode = "fixture" if fixture else "live"
    try:
        stats = await ingest_edgar_13f(conn, client, curated=curated)
    except AdapterUnavailable as exc:
        return "unavailable", f"[{mode}] {exc}"
    if stats.filers_failed:
        return "ok", f"[{mode}] partial: {stats.summary()}"
    return "ok", f"[{mode}] {stats.summary()}"


async def _run_gdelt(conn) -> tuple[str, str]:
    """GDELT headlines + VADER sentiment -> news_item. Curated query phrases
    from config (the #9 precedent); fixture mode keeps CI off the live GDELT
    DOC API (R-01) and skips pacing (nothing to pace). NOTE: the summary's
    `failed_tickers=` token is machine-parsed by signals/news.news_signal —
    it must survive into pipeline_run.message verbatim. All-tickers-failed
    raises AdapterUnavailable inside ingest_gdelt (source effectively down)."""
    settings = get_settings()
    queries = load_news_queries(settings.news_queries_path)
    fixture = settings.gdelt_fixture_mode
    client = FixtureGdeltClient() if fixture else RealGdeltClient()
    mode = "fixture" if fixture else "live"
    try:
        stats = await ingest_gdelt(conn, client, queries=queries,
                                   sleeper=_no_pacing if fixture else asyncio.sleep)
    except AdapterUnavailable as exc:
        return "unavailable", f"[{mode}] {exc}"
    if stats.failed_symbols:
        return "ok", f"[{mode}] partial: {stats.summary()}"
    return "ok", f"[{mode}] {stats.summary()}"


async def _run_engine_stage(conn, run_date: date) -> tuple[str, str]:
    """Task #10: signal calculators + recommendation writes (contract §1–§10).
    Runs after ingestion so it scores today's freshly upserted facts."""
    stats = await run_engine(conn, run_date)
    if stats.tickers_scored == 0:
        return "unavailable", f"no ticker had scoreable data: {stats.summary()}"
    return "ok", stats.summary()


async def run_pipeline_once(run_date: date | None = None) -> None:
    run_date = run_date or datetime.now(UTC).date()
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Ingestion sources first, then the derived engine stage (deck §22.1
        # ingest → signal → score → persist). Same isolation per stage.
        for source in (*KNOWN_SOURCES, ENGINE_SOURCE):
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
                elif source == "twse_tpex":
                    status, message = await _run_twse_tpex(conn)
                elif source == "edgar_13f":
                    status, message = await _run_edgar_13f(conn)
                elif source == "gdelt":
                    status, message = await _run_gdelt(conn)
                else:  # ENGINE_SOURCE — the loop enumerates exactly these 5
                    status, message = await _run_engine_stage(conn, run_date)
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
