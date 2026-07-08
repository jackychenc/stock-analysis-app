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
"""

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

logger = logging.getLogger(__name__)

BACKFILL_PERIOD = "2y"     # first run per ticker: backtest needs >=12-24mo (§25/7)
INCREMENTAL_PERIOD = "7d"  # daily runs: short overlap window, upserts dedupe

# A8 #1: allowlist at the adapter boundary — symbols feed outbound URL
# construction, so a bad ticker row must never shape an egress request.
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")

# A8 #4: bounds beyond NaN — poisoned upstream values must not land in the DB.
_MAX_PRICE = Decimal("10000000")      # no listed equity trades at 10M/share
_MAX_VOLUME = 10_000_000_000_000      # 10T shares/day is not a real market

# A8 #3 (mandatory after A7's live 429 pre-flight): explicit outbound timeout,
# paced requests, bounded retries w/ exponential backoff + jitter — no storms.
REQUEST_TIMEOUT_S = 15
PACING_DELAY_S = 2.0        # pause between tickers — stay under Yahoo's radar
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_S = 1.0
_TRANSIENT_MARKERS = ("429", "too many requests", "rate limit",
                      "500", "502", "503", "504", "timed out", "timeout")


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


async def _with_retries(fn, /, *args, sleeper=asyncio.sleep, rng=None) -> Any:
    """Run blocking client call in a worker thread; retry transient failures
    (429/5xx/timeouts) with exponential backoff + jitter. Non-transient errors
    raise immediately (per-ticker isolation handles them). Exhausted retries
    re-raise the transient error — the source then reads 'unavailable', never
    a tight-loop hammer (R-01)."""
    import random

    rng = rng or random.random
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as exc:
            if attempt == _RETRY_ATTEMPTS or not _is_transient(exc):
                raise
            logger.warning("transient upstream error (attempt %d/%d): %s",
                           attempt, _RETRY_ATTEMPTS, exc)
            base = _RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
            await sleeper(base * (1 + rng() * 0.5))  # jitter: 1.0x..1.5x


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

        frame = yf.Ticker(symbol).history(period=period, interval="1d",
                                          auto_adjust=False,
                                          timeout=REQUEST_TIMEOUT_S)
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

        return dict(yf.Ticker(symbol).get_info(timeout=REQUEST_TIMEOUT_S) or {})


class FixtureYFinanceClient:
    """Deterministic fixture mode (FR-19 / A6 bucket 4): reproducible synthetic
    data, zero network — for CI and stack smoke without touching Yahoo (R-01).
    Values derive only from (symbol, date), so re-runs are byte-stable."""

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


class AdapterUnavailable(Exception):
    """Raised when the source produced no usable data at all — the pipeline
    marks the whole source 'unavailable' (never a partial silent success)."""


@dataclass
class IngestStats:
    tickers_ok: int = 0
    tickers_failed: int = 0
    bars_upserted: int = 0
    bars_skipped: int = 0        # NaN/incomplete rows — counted, not hidden
    fundamentals_upserted: int = 0
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        msg = (f"tickers ok={self.tickers_ok} failed={self.tickers_failed}; "
               f"bars upserted={self.bars_upserted} skipped={self.bars_skipped}; "
               f"fundamentals={self.fundamentals_upserted}")
        if self.failures:
            msg += f"; failures: {'; '.join(self.failures)}"
        return msg


def _num(value: Any) -> Decimal | None:
    """Decimal-safe conversion; NaN/None/garbage -> None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return Decimal(str(value)) if not isinstance(value, float) else Decimal(repr(f))


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return int(f)


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

_UPSERT_BAR = """
    INSERT INTO price_bar (ticker_id, bar_date, open, high, low, close, volume, source)
    VALUES ($1, $2, $3, $4, $5, $6, $7, 'yfinance')
    ON CONFLICT (ticker_id, bar_date) DO UPDATE SET
        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
        close = EXCLUDED.close, volume = EXCLUDED.volume, source = EXCLUDED.source
"""

_UPSERT_FUNDAMENTAL = """
    INSERT INTO fundamental (ticker_id, asof_date, pe, pb, ev_ebitda, revenue,
                             eps, gross_margin, op_margin, net_margin)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ON CONFLICT (ticker_id, asof_date) DO UPDATE SET
        pe = EXCLUDED.pe, pb = EXCLUDED.pb, ev_ebitda = EXCLUDED.ev_ebitda,
        revenue = EXCLUDED.revenue, eps = EXCLUDED.eps,
        gross_margin = EXCLUDED.gross_margin, op_margin = EXCLUDED.op_margin,
        net_margin = EXCLUDED.net_margin
"""


async def ingest_yfinance(
    conn: Any,
    client: YFinanceClient,
    *,
    asof: date | None = None,
    sleeper=asyncio.sleep,
) -> IngestStats:
    """Ingest all covered tickers. Raises AdapterUnavailable only when NOTHING
    succeeded; partial failures are reported in stats (honest, not fatal)."""
    stats = IngestStats()
    asof = asof or date.today()

    tickers = await conn.fetch(
        "SELECT id, full_symbol FROM ticker WHERE is_covered ORDER BY id"
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


async def _ingest_one(
    conn: Any,
    client: YFinanceClient,
    ticker_id: int,
    symbol: str,
    asof: date,
    stats: IngestStats,
    sleeper=asyncio.sleep,
) -> None:
    # A8 #1: allowlist before any egress. fullmatch (Y-1): `$` would admit a
    # trailing newline — this regex IS the SSRF boundary, so match exactly.
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol rejected by egress allowlist")

    have_bars = await conn.fetchval(
        "SELECT count(*) FROM price_bar WHERE ticker_id = $1", ticker_id
    )
    period = INCREMENTAL_PERIOD if have_bars else BACKFILL_PERIOD

    bars = await _with_retries(client.fetch_daily_bars, symbol, period,
                               sleeper=sleeper)
    rows = []
    for bar in bars:
        o, h, lo, c = (_num(bar.get(k)) for k in ("open", "high", "low", "close"))
        vol = _int(bar.get("volume"))
        if (None in (o, h, lo, c) or bar.get("date") is None
                or not _valid_bar_values(o, h, lo, c, vol)):
            stats.bars_skipped += 1  # incomplete/out-of-bounds: skip loudly
            continue
        rows.append((ticker_id, bar["date"], o, h, lo, c, vol))
    if not rows:
        raise ValueError(f"no usable price bars returned (period={period})")
    await conn.executemany(_UPSERT_BAR, rows)
    stats.bars_upserted += len(rows)

    info = await _with_retries(client.fetch_fundamentals, symbol, sleeper=sleeper)
    values = [_num(info.get(key)) for key in _FUNDAMENTAL_FIELDS.values()]
    # A fundamentals snapshot with every field missing is not a snapshot —
    # record bars (done above) but flag the fundamentals gap explicitly.
    if all(v is None for v in values):
        stats.failures.append(f"{symbol}: fundamentals empty (bars ok)")
        return
    await conn.execute(_UPSERT_FUNDAMENTAL, ticker_id, asof, *values)
    stats.fundamentals_upserted += 1
