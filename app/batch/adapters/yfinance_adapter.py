"""yfinance ingestion adapter — roadmap Step 2 (task #8).

Writes daily OHLCV -> price_bar and a fundamentals snapshot -> fundamental
for every covered ticker. Scope guardrails (Cindy, task #8):
- ingestion ONLY: `score` columns stay NULL (signal calculators = task #10);
- per-ticker isolation: one ticker's failure never aborts the others (§22.4);
- idempotent upserts: re-runs update, never duplicate;
- decimal-safe: floats go through Decimal(str(x)) before NUMERIC binds;
- data quality is explicit: NaN/missing values are skipped/NULLed and COUNTED
  in the returned stats — no silent degradation (FR-34 spirit).

The network client is injected (YFinanceClient protocol) so unit tests run a
deterministic fake; RealYFinanceClient is the only code that touches the
`yfinance` package (imported lazily) — R-01: unofficial, ToS-gray source.

Task #9 extracted the reviewed shared controls (AdapterUnavailable, retries,
decimal-safe converters, egress allowlist) into adapters/common.py; this
module re-exports the same public API so nothing upstream changes.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from app.batch.adapters.common import (
    PACING_DELAY_S,
    AdapterUnavailable,
    _int,
    _num,
    _with_retries,
    check_symbol,
)

__all__ = [
    "BENCHMARK_SYMBOLS",
    "PACING_DELAY_S",
    "AdapterUnavailable",
    "FixtureYFinanceClient",
    "IngestStats",
    "RealYFinanceClient",
    "YFinanceClient",
    "check_benchmark_symbol",
    "ingest_yfinance",
]

logger = logging.getLogger(__name__)

BACKFILL_PERIOD = "2y"     # first run per ticker: backtest needs >=12-24mo (§25/7)
INCREMENTAL_PERIOD = "7d"  # daily runs: short overlap window, upserts dedupe

# Task #13: backtest honesty (contract §10) needs ^TWII / ^GSPC daily bars.
# The caret prefix is deliberately OUTSIDE SYMBOL_RE — index symbols are
# validated by EXACT membership in this curated allowlist (stronger than a
# regex), never by loosening the covered-ticker egress boundary (A8 #1).
BENCHMARK_SYMBOLS = ("^TWII", "^GSPC")
# exchange labels: schema.sql constrains ticker.exchange only by convention
# ('TWSE' | 'TPEx' | 'US') — the TW index rides 'TWSE', the US index 'US'.
_BENCHMARK_META = {
    "^TWII": ("TWSE", "TAIEX (TW benchmark index)"),
    "^GSPC": ("US", "S&P 500 (US benchmark index)"),
}

# A8 #4: bounds beyond NaN — poisoned upstream values must not land in the DB.
_MAX_PRICE = Decimal("10000000")      # no listed equity trades at 10M/share
_MAX_VOLUME = 10_000_000_000_000      # 10T shares/day is not a real market


class YFinanceClient(Protocol):
    def fetch_daily_bars(self, symbol: str, period: str) -> list[dict[str, Any]]:
        """Return [{date, open, high, low, close, volume}, ...] (floats/ints)."""
        ...

    def fetch_fundamentals(self, symbol: str) -> dict[str, Any]:
        """Return the raw info mapping (trailingPE, priceToBook, ...)."""
        ...


class RealYFinanceClient:
    """The only yfinance-touching code. Sync (yfinance is sync); the adapter
    runs it in a worker thread. Egress is pinned to yfinance's built-in Yahoo
    endpoints — deliberately NO configurable base host (A8 #2)."""

    def fetch_daily_bars(self, symbol: str, period: str) -> list[dict[str, Any]]:
        import yfinance as yf

        # yfinance 1.x dropped the per-call timeout kwarg (G3 finding, A7 live
        # run): the lib bounds its own HTTP internally; the adapter layer keeps
        # bounded retries + pacing, and a hard await-timeout is a #16
        # ops-hardening item (A8 lane). Passing timeout= raises TypeError.
        frame = yf.Ticker(symbol).history(period=period, interval="1d",
                                          auto_adjust=False)
        bars: list[dict[str, Any]] = []
        for ts, row in frame.iterrows():
            bars.append({
                "date": ts.date(),
                "open": row.get("Open"),
                "high": row.get("High"),
                "low": row.get("Low"),
                "close": row.get("Close"),
                "volume": row.get("Volume"),
            })
        return bars

    def fetch_fundamentals(self, symbol: str) -> dict[str, Any]:
        import yfinance as yf

        return dict(yf.Ticker(symbol).get_info() or {})  # 1.x: no kwargs


class FixtureYFinanceClient:
    """Deterministic fixture mode (FR-19 / A6 bucket 4): reproducible synthetic
    data, zero network — for CI and stack smoke without touching Yahoo (R-01).
    Values derive only from (symbol, date), so re-runs are byte-stable.
    Benchmark symbols (^TWII/^GSPC, task #13) flow through the SAME generator —
    2y of stable daily-ish bars, so containerized/CI gates stay offline."""

    def fetch_daily_bars(self, symbol: str, period: str) -> list[dict[str, Any]]:
        from datetime import timedelta

        seed = sum(ord(c) for c in symbol)
        base = 50 + (seed % 900)  # per-symbol stable base price
        days = 500 if period == BACKFILL_PERIOD else 7
        end = date.today()
        bars = []
        for i in range(days):
            d = end - timedelta(days=days - 1 - i)
            if d.weekday() >= 5:  # skip weekends like a real market
                continue
            wave = (i * seed) % 17 - 8  # deterministic wobble
            close = base + wave
            bars.append({
                "date": d,
                "open": close - 1, "high": close + 2, "low": close - 2,
                "close": close, "volume": 1_000_000 + (seed * i) % 500_000,
            })
        return bars

    def fetch_fundamentals(self, symbol: str) -> dict[str, Any]:
        seed = sum(ord(c) for c in symbol)
        return {
            "trailingPE": 10 + seed % 25,
            "priceToBook": 1 + (seed % 40) / 10,
            "enterpriseToEbitda": 8 + seed % 12,
            "totalRevenue": 1_000_000_000 + seed * 1_000_000,
            "trailingEps": 5 + seed % 40,
            "grossMargins": 0.35 + (seed % 30) / 100,
            "operatingMargins": 0.20 + (seed % 20) / 100,
            "profitMargins": 0.10 + (seed % 15) / 100,
        }


@dataclass
class IngestStats:
    tickers_ok: int = 0
    tickers_failed: int = 0
    bars_upserted: int = 0
    bars_skipped: int = 0        # NaN/incomplete rows — counted, not hidden
    fundamentals_upserted: int = 0
    benchmarks_ok: int = 0       # task #13: index bars counted SEPARATELY so
    benchmarks_failed: int = 0   # the run message stays honest per population
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        msg = (f"tickers ok={self.tickers_ok} failed={self.tickers_failed}; "
               f"bars upserted={self.bars_upserted} skipped={self.bars_skipped}; "
               f"fundamentals={self.fundamentals_upserted}; "
               f"benchmarks ok={self.benchmarks_ok} failed={self.benchmarks_failed}")
        if self.failures:
            msg += f"; failures: {'; '.join(self.failures)}"
        return msg


# fundamental column <- yfinance info key
_FUNDAMENTAL_FIELDS = {
    "pe": "trailingPE",
    "pb": "priceToBook",
    "ev_ebitda": "enterpriseToEbitda",
    "revenue": "totalRevenue",
    "eps": "trailingEps",
    "gross_margin": "grossMargins",
    "op_margin": "operatingMargins",
    "net_margin": "profitMargins",
}

# v1.2.5 provenance: ingested_at defaults to now() on INSERT and is explicitly
# refreshed on UPDATE (last-fetched semantics, per A3's upsert rule).
_UPSERT_BAR = """
    INSERT INTO price_bar (ticker_id, bar_date, open, high, low, close, volume, source)
    VALUES ($1, $2, $3, $4, $5, $6, $7, 'yfinance')
    ON CONFLICT (ticker_id, bar_date) DO UPDATE SET
        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
        close = EXCLUDED.close, volume = EXCLUDED.volume,
        source = EXCLUDED.source, ingested_at = now()
"""

_UPSERT_FUNDAMENTAL = """
    INSERT INTO fundamental (ticker_id, asof_date, pe, pb, ev_ebitda, revenue,
                             eps, gross_margin, op_margin, net_margin, source)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'yfinance')
    ON CONFLICT (ticker_id, asof_date) DO UPDATE SET
        pe = EXCLUDED.pe, pb = EXCLUDED.pb, ev_ebitda = EXCLUDED.ev_ebitda,
        revenue = EXCLUDED.revenue, eps = EXCLUDED.eps,
        gross_margin = EXCLUDED.gross_margin, op_margin = EXCLUDED.op_margin,
        net_margin = EXCLUDED.net_margin,
        source = EXCLUDED.source, ingested_at = now()
"""


async def ingest_yfinance(
    conn: Any,
    client: YFinanceClient,
    *,
    asof: date | None = None,
    sleeper=asyncio.sleep,
    only_ticker: str | None = None,
) -> IngestStats:
    """Ingest all covered tickers. Raises AdapterUnavailable only when NOTHING
    succeeded; partial failures are reported in stats (honest, not fatal).
    Task #20 (ADR-009): only_ticker narrows the covered-ticker query to one
    full_symbol for an on-demand run; None (the daily batch) is unchanged."""
    stats = IngestStats()
    asof = asof or date.today()

    if only_ticker is None:
        tickers = await conn.fetch(
            "SELECT id, full_symbol FROM ticker WHERE is_covered ORDER BY id"
        )
    else:
        tickers = await conn.fetch(
            "SELECT id, full_symbol FROM ticker WHERE is_covered AND full_symbol = $1"
            " ORDER BY id",
            only_ticker,
        )
    if not tickers:
        raise AdapterUnavailable("no covered tickers to ingest")

    for i, t in enumerate(tickers):
        ticker_id, symbol = t["id"], t["full_symbol"]
        if i > 0:
            await sleeper(PACING_DELAY_S)  # paced egress between tickers
        try:
            await _ingest_one(conn, client, ticker_id, symbol, asof, stats,
                              sleeper=sleeper)
            stats.tickers_ok += 1
        except Exception as exc:  # per-ticker isolation (§22.4)
            stats.tickers_failed += 1
            stats.failures.append(f"{symbol}: {exc}")
            # A8 #6 log hygiene: source+ticker+status only, never bodies.
            logger.warning("yfinance ingest failed for %s: %s", symbol, exc)

    if stats.tickers_ok == 0:
        raise AdapterUnavailable(f"all tickers failed: {'; '.join(stats.failures)}")

    if only_ticker is not None:
        # On-demand runs are per-ticker (task #20): the GLOBAL benchmark bars
        # belong to the nightly batch (task #13) — no extra egress here.
        return stats

    # Task #13: benchmarks AFTER the covered universe — their bars feed the
    # backtest stage only (is_covered=FALSE, never scored). Same per-symbol
    # isolation; failures are counted in the separate benchmark counters.
    for symbol in BENCHMARK_SYMBOLS:
        await sleeper(PACING_DELAY_S)  # covered fetches already ran — keep pacing
        try:
            await _ingest_benchmark(conn, client, symbol, stats, sleeper=sleeper)
            stats.benchmarks_ok += 1
        except Exception as exc:
            stats.benchmarks_failed += 1
            stats.failures.append(f"{symbol}: {exc}")
            logger.warning("yfinance benchmark ingest failed for %s: %s", symbol, exc)
    return stats


def _valid_bar_values(o: Decimal, h: Decimal, lo: Decimal, c: Decimal,
                      vol: int | None) -> bool:
    """A8 #4: prices must be positive and sane; volume non-negative and sane.
    (Fundamentals like EPS may legitimately be negative — bounds apply to
    bars only; negative EPS is stored honestly, per A6 bucket 2.)"""
    for price in (o, h, lo, c):
        if price <= 0 or price > _MAX_PRICE:
            return False
    if vol is not None and (vol < 0 or vol > _MAX_VOLUME):
        return False
    return True


def _usable_bar_rows(bars: list[dict[str, Any]], ticker_id: int,
                     stats: IngestStats) -> list[tuple]:
    """Decimal-safe bar rows for _UPSERT_BAR; incomplete/out-of-bounds rows
    are skipped LOUDLY (counted in stats — FR-34 spirit)."""
    rows = []
    for bar in bars:
        o, h, lo, c = (_num(bar.get(k)) for k in ("open", "high", "low", "close"))
        vol = _int(bar.get("volume"))
        if (None in (o, h, lo, c) or bar.get("date") is None
                or not _valid_bar_values(o, h, lo, c, vol)):
            stats.bars_skipped += 1  # incomplete/out-of-bounds: skip loudly
            continue
        rows.append((ticker_id, bar["date"], o, h, lo, c, vol))
    return rows


async def _fetch_and_upsert_bars(
    conn: Any,
    client: YFinanceClient,
    ticker_id: int,
    symbol: str,
    stats: IngestStats,
    sleeper=asyncio.sleep,
) -> None:
    """Backfill-or-incremental daily bars -> price_bar (covered + benchmark)."""
    have_bars = await conn.fetchval(
        "SELECT count(*) FROM price_bar WHERE ticker_id = $1", ticker_id
    )
    period = INCREMENTAL_PERIOD if have_bars else BACKFILL_PERIOD

    bars = await _with_retries(client.fetch_daily_bars, symbol, period,
                               sleeper=sleeper)
    rows = _usable_bar_rows(bars, ticker_id, stats)
    if not rows:
        raise ValueError(f"no usable price bars returned (period={period})")
    await conn.executemany(_UPSERT_BAR, rows)
    stats.bars_upserted += len(rows)


async def _ingest_one(
    conn: Any,
    client: YFinanceClient,
    ticker_id: int,
    symbol: str,
    asof: date,
    stats: IngestStats,
    sleeper=asyncio.sleep,
) -> None:
    # A8 #1 / Y-1: fullmatch allowlist before any egress (common.check_symbol).
    check_symbol(symbol)

    await _fetch_and_upsert_bars(conn, client, ticker_id, symbol, stats,
                                 sleeper=sleeper)

    info = await _with_retries(client.fetch_fundamentals, symbol, sleeper=sleeper)
    values = [_num(info.get(key)) for key in _FUNDAMENTAL_FIELDS.values()]
    # A fundamentals snapshot with every field missing is not a snapshot —
    # record bars (done above) but flag the fundamentals gap explicitly.
    if all(v is None for v in values):
        stats.failures.append(f"{symbol}: fundamentals empty (bars ok)")
        return
    await conn.execute(_UPSERT_FUNDAMENTAL, ticker_id, asof, *values)
    stats.fundamentals_upserted += 1


# --- market benchmarks (task #13) --------------------------------------------

def check_benchmark_symbol(symbol: str) -> None:
    """A8 #1 for index symbols: EXACT membership in the curated allowlist
    BEFORE any egress. Deliberately not SYMBOL_RE — the caret stays outside
    the covered-ticker regex; anything not literally listed is rejected."""
    if symbol not in BENCHMARK_SYMBOLS:
        raise ValueError("benchmark symbol rejected by egress allowlist")


# is_covered=FALSE: benchmark rows must never leak into scoring/dashboard —
# run_engine and every ticker-listing query filter on is_covered. sector stays
# NULL (an index has none; the coverage gate is is_covered, not sector).
_INSERT_BENCHMARK_TICKER = """
    INSERT INTO ticker (symbol, exchange, full_symbol, name, is_covered)
    VALUES ($1, $2, $3, $4, FALSE)
    ON CONFLICT (full_symbol) DO NOTHING
"""


async def _ingest_benchmark(
    conn: Any,
    client: YFinanceClient,
    symbol: str,
    stats: IngestStats,
    sleeper=asyncio.sleep,
) -> None:
    """Ensure the benchmark's ticker row exists, then upsert its daily bars
    exactly like a covered ticker's (same validation, same _UPSERT_BAR).
    No fundamentals — an index has none to snapshot."""
    check_benchmark_symbol(symbol)  # exact membership BEFORE any egress
    exchange, name = _BENCHMARK_META[symbol]
    await conn.execute(_INSERT_BENCHMARK_TICKER, symbol, exchange, symbol, name)
    ticker_id = await conn.fetchval(
        "SELECT id FROM ticker WHERE full_symbol = $1", symbol
    )
    await _fetch_and_upsert_bars(conn, client, ticker_id, symbol, stats,
                                 sleeper=sleeper)
