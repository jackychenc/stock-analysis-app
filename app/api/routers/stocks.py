from enum import IntEnum

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import current_user
from app.db.pool import get_pool
from app.db.redis import get_redis
from app.schemas.contracts import (
    BacktestResult,
    Dashboard,
    ModuleDetail,
    ModuleStatus,
    SupplyChainGraph,
)
from app.services import analysis_jobs, read_service
from app.services.backtest_engine import compute_ticker_backtest

router = APIRouter(prefix="/stocks", tags=["stocks"], dependencies=[Depends(current_user)])


async def _covered_ticker_or_404(conn, full_symbol: str):
    """Unknown or uncovered ticker => 404 SECTOR_NOT_COVERED (openapi.yaml)."""
    ticker = await read_service.fetch_ticker(conn, full_symbol)
    if ticker is None or not ticker["is_covered"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SECTOR_NOT_COVERED",
                    "message": "Ticker not covered in MVP scope."},
        )
    return ticker


@router.get("/{ticker}/dashboard", response_model=Dashboard)
async def dashboard(ticker: str) -> Dashboard:
    """Single-row snapshot read (NFR-02: no live computation on request path)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _covered_ticker_or_404(conn, ticker)
        dash = await read_service.fetch_dashboard(conn, row)
    # ADR-009 next_refresh_at: cooldown state lives in Redis; a Redis hiccup
    # degrades to null (Refresh appears available; the POST still enforces
    # the cooldown server-side) rather than failing the snapshot read.
    try:
        r = await get_redis()
        dash.next_refresh_at = await analysis_jobs.next_refresh_at(
            r, row["full_symbol"])
    except Exception:  # noqa: BLE001 — availability of the snapshot wins
        pass
    return dash


def _module_detail_stub(module: str) -> ModuleDetail:
    """Lens-detail series are populated by ingestion (tasks #8/#9/#12)."""
    return ModuleDetail(module=module, status=ModuleStatus.unavailable, series=[])


@router.get("/{ticker}/technical", response_model=ModuleDetail)
async def technical(ticker: str) -> ModuleDetail:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _covered_ticker_or_404(conn, ticker)
    return _module_detail_stub("technical")


@router.get("/{ticker}/fundamentals", response_model=ModuleDetail)
async def fundamentals(ticker: str) -> ModuleDetail:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _covered_ticker_or_404(conn, ticker)
    return _module_detail_stub("fundamental")


@router.get("/{ticker}/news", response_model=ModuleDetail)
async def news(ticker: str) -> ModuleDetail:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _covered_ticker_or_404(conn, ticker)
    return _module_detail_stub("news")


@router.get("/{ticker}/chip", response_model=ModuleDetail)
async def chip(ticker: str) -> ModuleDetail:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _covered_ticker_or_404(conn, ticker)
    return _module_detail_stub("chip")


@router.get("/{ticker}/supply-chain", response_model=SupplyChainGraph)
async def supply_chain(ticker: str) -> SupplyChainGraph:
    """Discovery layer (never scored). Graph populated in Step 5 (task #11)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _covered_ticker_or_404(conn, ticker)
    return SupplyChainGraph()


class WindowMonths(IntEnum):
    """openapi.yaml: window_months enum [3, 6, 12] — an IntEnum so FastAPI
    coerces AND validates the query string (Literal[int] would 422 on '12')."""

    quarter = 3
    half = 6
    year = 12


@router.get("/{ticker}/backtest", response_model=BacktestResult)
async def backtest(
    ticker: str, window_months: WindowMonths = Query(WindowMonths.half)
) -> BacktestResult:
    """Task #13: TICKER-SCOPED backtest computed at request time (contract
    §10 honesty rules; <12mo history honestly reports insufficient_history).
    An A3 scope ruling on serving the GLOBAL backtest_result rows instead is
    pending — the choice is isolated inside compute_ticker_backtest, so the
    swap is a one-function change here."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _covered_ticker_or_404(conn, ticker)
        return await compute_ticker_backtest(conn, row,
                                             window_months=int(window_months))
