from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import current_user
from app.core.config import get_settings
from app.db.pool import get_pool
from app.schemas.contracts import (
    BacktestResult,
    Dashboard,
    ModuleDetail,
    ModuleStatus,
    SupplyChainGraph,
)
from app.services import read_service

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
        return await read_service.fetch_dashboard(conn, row)


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


@router.get("/{ticker}/backtest", response_model=BacktestResult)
async def backtest(
    ticker: str, window_months: int = Query(6, enum=[3, 6, 12])
) -> BacktestResult:
    """Backtest engine lands in Step 7 (task #13); until >=12mo of history is
    backfilled this honestly reports insufficient_history=true (nfr-budgets.md)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _covered_ticker_or_404(conn, ticker)
    settings = get_settings()
    return BacktestResult(
        window_months=window_months,
        benchmark=read_service.benchmark_for(row["exchange"]),
        insufficient_history=True,
        methodology_version=settings.methodology_version,
        disclaimer=settings.disclaimer_text,  # FR-39: config-sourced
        disclaimer_version=settings.disclaimer_version,
    )
