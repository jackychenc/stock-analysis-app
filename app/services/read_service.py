"""Read Service — snapshot lookups for the dashboard (deck §19 DOMAIN layer).

Foundation scope: real ticker lookup + latest-recommendation read. Ingestion
and scoring write the data in later steps (tasks #8-#13); until the first
batch runs, dashboards legitimately return rec_date=None with all modules
'unavailable' — the contract shape is fully exercised (repo-layout.md §Foundation).
"""

import json
from datetime import UTC, datetime, time, timedelta
from typing import Any

import asyncpg

# Read-only reuse of the news lens's pure helpers (task #14): the SAME window
# and failed_tickers token semantics as the scoring read — never a fork.
from app.batch.signals.news import WINDOW_DAYS, parse_failed_tickers
from app.core.config import get_settings
from app.schemas.contracts import (
    SCORING_MODULES,
    Dashboard,
    DashboardModules,
    ModuleDetail,
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

    signal = row["composite_signal"]
    rec = Recommendation(
        composite_signal=float(signal) if signal is not None else None,
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
        disclaimer=get_settings().disclaimer_text,  # FR-39: config-sourced
        disclaimer_version=get_settings().disclaimer_version,
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
        disclaimer=get_settings().disclaimer_text,  # FR-39: config-sourced
        disclaimer_version=get_settings().disclaimer_version,
    )


# ---------------------------------------------------------------------------
# Lens-detail reads (task #14) — read-only fact queries into the frozen
# ModuleDetail envelope (contract v1.2.1). NFR-02: no computation on the
# request path — scores/indicators are served exactly as the batch persisted
# them; status is never fabricated (no rows -> unavailable + empty series).
# ---------------------------------------------------------------------------

TECHNICAL_SERIES_BARS = 60   # ~one MA60 window of daily OHLCV for charting
CHIP_TW_SERIES_DAYS = 30     # ~one month of TW 3-institution facts
CHIP_US_QUARTERS = 8         # ~two years of 13F quarter aggregates
NEWS_SERIES_CAP = 50


def _f(value: Any) -> float | None:
    """Decimal->float at the response edge only (contract numerics note)."""
    return None if value is None else float(value)


async def fetch_technical_detail(
    conn: asyncpg.Connection, ticker: asyncpg.Record
) -> ModuleDetail:
    bars = await conn.fetch(
        "SELECT bar_date, open, high, low, close, volume FROM price_bar"
        " WHERE ticker_id = $1 ORDER BY bar_date DESC LIMIT $2",
        ticker["id"], TECHNICAL_SERIES_BARS,
    )
    series: list[dict] = [
        {
            "date": b["bar_date"].isoformat(),
            "open": _f(b["open"]), "high": _f(b["high"]), "low": _f(b["low"]),
            "close": _f(b["close"]), "volume": b["volume"],
        }
        for b in reversed(bars)  # ascending date for charting
    ]
    ind = await conn.fetchrow(
        "SELECT calc_date, ma20, ma60, rsi14, macd, macd_signal, macd_hist, score"
        " FROM technical_indicator WHERE ticker_id = $1"
        " ORDER BY calc_date DESC LIMIT 1",
        ticker["id"],
    )
    if ind is not None and bars:
        latest = {
            k: _f(ind[k])
            for k in ("ma20", "ma60", "rsi14", "macd", "macd_signal", "macd_hist")
        }
        ind_date = ind["calc_date"].isoformat()
        if series[-1]["date"] == ind_date:
            series[-1].update(latest)  # fold indicators onto their bar
        else:
            series.append({"date": ind_date, **latest})
    if not bars:
        return ModuleDetail(module="technical", status=ModuleStatus.unavailable,
                            series=[])
    return ModuleDetail(
        module="technical",
        status=ModuleStatus.ok,
        signal_score=_f(ind["score"]) if ind is not None else None,
        as_of=ind["calc_date"] if ind is not None else bars[0]["bar_date"],
        series=series,
    )


async def fetch_fundamental_detail(
    conn: asyncpg.Connection, ticker: asyncpg.Record
) -> ModuleDetail:
    row = await conn.fetchrow(
        "SELECT asof_date, pe, pb, ev_ebitda, revenue, eps,"
        "       gross_margin, op_margin, net_margin, score"
        " FROM fundamental WHERE ticker_id = $1"
        " ORDER BY asof_date DESC LIMIT 1",
        ticker["id"],
    )
    if row is None:
        return ModuleDetail(module="fundamental", status=ModuleStatus.unavailable,
                            series=[])
    snapshot: dict = {"as_of": row["asof_date"].isoformat()}
    snapshot.update({
        k: _f(row[k])
        for k in ("pe", "pb", "ev_ebitda", "revenue", "eps",
                  "gross_margin", "op_margin", "net_margin")
    })
    return ModuleDetail(
        module="fundamental",
        status=ModuleStatus.ok,
        signal_score=_f(row["score"]),
        as_of=row["asof_date"],
        series=[snapshot],
    )


async def fetch_chip_detail(
    conn: asyncpg.Connection, ticker: asyncpg.Record
) -> ModuleDetail:
    """Market-routed facts (FR-14/16): TW 3-institution dailies; US 13F
    quarter aggregates. The detail page shows FACTS — a single US quarter is
    still served as its one quarter (the flow-lens unavailability honesty
    lives in the recommendation breakdown, task #21), nothing is faked."""
    if ticker["exchange"] in ("TWSE", "TPEx"):
        rows = await conn.fetch(
            "SELECT trade_date, foreign_net, investment_trust_net, dealer_net,"
            "       margin_balance, block_trade_volume, score"
            " FROM chip_data_tw WHERE ticker_id = $1"
            " ORDER BY trade_date DESC LIMIT $2",
            ticker["id"], CHIP_TW_SERIES_DAYS,
        )
        series = [
            {
                "trade_date": r["trade_date"].isoformat(),
                "foreign_net": r["foreign_net"],
                "investment_trust_net": r["investment_trust_net"],
                "dealer_net": r["dealer_net"],
                "margin_balance": r["margin_balance"],
                "block_trade_volume": r["block_trade_volume"],
                "score": _f(r["score"]),
            }
            for r in reversed(rows)  # ascending date for charting
        ]
        # Latest row the batch actually scored — never derived here.
        score = next((_f(r["score"]) for r in rows if r["score"] is not None), None)
        as_of = rows[0]["trade_date"] if rows else None
    else:
        rows = await conn.fetch(
            "SELECT quarter, sum(shares) AS total_shares, count(*) AS filer_count"
            " FROM institutional_position_us"
            " WHERE ticker_id = $1 AND shares IS NOT NULL"
            " GROUP BY quarter ORDER BY quarter DESC LIMIT $2",
            ticker["id"], CHIP_US_QUARTERS,
        )
        series = [
            {
                "quarter": r["quarter"].isoformat(),
                "total_shares": int(r["total_shares"]),
                "filer_count": int(r["filer_count"]),
            }
            for r in reversed(rows)  # ascending quarters
        ]
        score = None
        if rows:
            score = _f(await conn.fetchval(
                "SELECT score FROM institutional_position_us"
                " WHERE ticker_id = $1 AND score IS NOT NULL"
                " ORDER BY quarter DESC LIMIT 1",
                ticker["id"],
            ))
        as_of = rows[0]["quarter"] if rows else None
    return ModuleDetail(
        module="chip",
        status=ModuleStatus.ok if rows else ModuleStatus.unavailable,
        signal_score=score,
        as_of=as_of,
        series=series,
    )


async def fetch_news_detail(
    conn: asyncpg.Connection, ticker: asyncpg.Record
) -> ModuleDetail:
    """Contract v1.2.8 §4a ternary at the read edge too: status derives from
    the FETCH OUTCOME (latest scheduled gdelt pipeline_run row + its
    failed_tickers token), never the row count — fetch-ok with 0 headlines is
    an honest ok/empty ("no news is neutral news"). signal_score stays None:
    the news module score is a windowed mean the batch never persists per
    ticker, and NFR-02 forbids recomputing it on the request path."""
    run = await conn.fetchrow(
        "SELECT run_date, status, message FROM pipeline_run"
        " WHERE source_name = 'gdelt' AND run_kind = 'scheduled'"
        " ORDER BY run_date DESC LIMIT 1",
    )
    fetch_ok = (
        run is not None
        and run["status"] == "ok"
        and ticker["full_symbol"] not in parse_failed_tickers(run["message"])
    )
    if not fetch_ok:
        return ModuleDetail(module="news", status=ModuleStatus.unavailable,
                            series=[])
    # Same bounded window as the lens: [run_date-7d 00:00, run_date+1d 00:00) UTC.
    window_start = datetime.combine(
        run["run_date"] - timedelta(days=WINDOW_DAYS), time.min, tzinfo=UTC)
    window_end = datetime.combine(
        run["run_date"] + timedelta(days=1), time.min, tzinfo=UTC)
    rows = await conn.fetch(
        "SELECT published_at, headline, url, source_name, sentiment"
        " FROM news_item"
        " WHERE ticker_id = $1 AND published_at >= $2 AND published_at < $3"
        " ORDER BY published_at DESC LIMIT $4",
        ticker["id"], window_start, window_end, NEWS_SERIES_CAP,
    )
    series = [
        {
            "published_at": r["published_at"].isoformat(),
            "headline": r["headline"],
            "url": r["url"],
            "source_name": r["source_name"],
            "sentiment": _f(r["sentiment"]),
        }
        for r in rows
    ]
    return ModuleDetail(
        module="news",
        status=ModuleStatus.ok,
        as_of=run["run_date"],
        series=series,
    )


# benchmark_for moved to app.services.backtest_engine (task #13): the §10
# contract keys the benchmark off the ticker's full_symbol market suffix.
