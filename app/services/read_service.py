"""Read Service — snapshot lookups for the dashboard (deck §19 DOMAIN layer).

Foundation scope: real ticker lookup + latest-recommendation read. Ingestion
and scoring write the data in later steps (tasks #8-#13); until the first
batch runs, dashboards legitimately return rec_date=None with all modules
'unavailable' — the contract shape is fully exercised (repo-layout.md §Foundation).
"""

import json
from typing import Any

import asyncpg

from app.schemas.contracts import (
    SCORING_MODULES,
    Dashboard,
    DashboardModules,
    ModuleStatus,
    ModuleSummary,
    PerModuleBreakdown,
    Recommendation,
    TargetPrice,
)


async def fetch_ticker(conn: asyncpg.Connection, full_symbol: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT id, full_symbol, symbol, exchange, name, sector, is_covered"
        " FROM ticker WHERE upper(full_symbol) = upper($1)",
        full_symbol,
    )


async def fetch_dashboard(conn: asyncpg.Connection, ticker: asyncpg.Record) -> Dashboard:
    row = await conn.fetchrow(
        """
        SELECT rec_date, composite_signal, composite_call,
               target_price_bear, target_price_base, target_price_bull,
               confidence_level, confidence_pct, conflict_flag, reduced_confidence,
               horizon_months, per_module_breakdown, data_completeness,
               methodology_version
        FROM recommendation
        WHERE ticker_id = $1
        ORDER BY rec_date DESC
        LIMIT 1
        """,
        ticker["id"],
    )
    if row is None:
        return _empty_dashboard(ticker["full_symbol"])

    breakdown_raw: list[dict[str, Any]] = json.loads(row["per_module_breakdown"])
    breakdown = [PerModuleBreakdown(**item) for item in breakdown_raw]

    summaries: dict[str, ModuleSummary] = {
        b.module: ModuleSummary(status=b.status, signal_score=b.signal_score)
        for b in breakdown
    }
    # Contract: all four scoring-module keys always present.
    for m in SCORING_MODULES:
        summaries.setdefault(m, ModuleSummary(status=ModuleStatus.unavailable))

    target = None
    if row["target_price_base"] is not None:
        target = TargetPrice(
            bear=float(row["target_price_bear"]) if row["target_price_bear"] is not None else None,
            base=float(row["target_price_base"]),
            bull=float(row["target_price_bull"]) if row["target_price_bull"] is not None else None,
        )

    rec = Recommendation(
        composite_signal=float(row["composite_signal"]),
        composite_call=row["composite_call"],
        target_price=target,
        confidence_level=row["confidence_level"],
        confidence_pct=float(row["confidence_pct"]) if row["confidence_pct"] is not None else None,
        conflict_flag=row["conflict_flag"],
        reduced_confidence=row["reduced_confidence"],
        horizon_months=row["horizon_months"],
        data_completeness=float(row["data_completeness"]),
        methodology_version=row["methodology_version"],
        per_module_breakdown=breakdown,
        suppressed_reason=(
            "Analysis Only — Insufficient Data"
            if row["composite_call"] == "SUPPRESSED"
            else None
        ),
    )
    return Dashboard(
        ticker=ticker["full_symbol"],
        rec_date=row["rec_date"],
        recommendation=rec,
        modules=DashboardModules(**summaries),
    )


def _empty_dashboard(full_symbol: str) -> Dashboard:
    """No snapshot yet (fresh install / pre-first-batch)."""
    unavailable = ModuleSummary(status=ModuleStatus.unavailable)
    return Dashboard(
        ticker=full_symbol,
        rec_date=None,
        recommendation=None,
        modules=DashboardModules(
            technical=unavailable, fundamental=unavailable,
            chip=unavailable, news=unavailable,
        ),
    )


def benchmark_for(exchange: str) -> str:
    return "^TWII" if exchange in ("TWSE", "TPEx") else "^GSPC"
