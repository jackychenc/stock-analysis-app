"""GDELT news ingestion adapter — roadmap Step 6 (task #12).

Writes 7-day headline + VADER sentiment facts -> news_item for every covered
ticker (GDELT DOC 2.0 API, English sources only — VADER is an English
lexicon). Scope guardrails:
- ingestion ONLY: the news signal calculator (signals/news.py) reads the
  stored rows; the item-level `score` column is informational (2x compound);
- per-ticker isolation: one ticker's query failure never aborts the others
  (§22.4) — but the failure is NAMED in the run message (see token below);
- idempotent inserts on (ticker_id, url, published_at): DO NOTHING — a
  headline is an immutable observation, re-runs never duplicate;
- sanitization ON INGEST (contract v1.2.8 §4a security bar): control chars
  (Cc/Cf) stripped, whitespace collapsed, NFC-normalized, 500-char cap —
  applied BEFORE both the DB store and the VADER input. HTML tags are kept
  as literal text (React escapes at render);
- URL validation: scheme must be http/https or the WHOLE item is dropped
  (javascript:/data: is adversarial; NULL urls would also accumulate
  duplicates under the UNIQUE(ticker_id, url, published_at) key);
- SSRF boundary (A8): egress is pinned to api.gdeltproject.org ONLY. The
  article URLs GDELT returns are stored as DATA and NEVER fetched.

MACHINE-STABLE COUPLING: when tickers failed, NewsIngestStats.summary() emits
the exact token `failed_tickers=SYM1,SYM2` (comma-joined, no spaces) at the
END of the message. signals/news.news_signal parses this token out of
pipeline_run.message to mark those tickers' news lens unavailable (contract
v1.2.8 §4a: module status derives from the FETCH outcome, never row count).
Change the token format only in lockstep with signals/news.py.
"""

import asyncio
import json
import logging
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

from app.batch.adapters.common import (
    PACING_DELAY_S,
    SYMBOL_RE,
    AdapterUnavailable,
    _with_retries,
    check_symbol,
    http_get,
)

logger = logging.getLogger(__name__)

# A8 #2 egress pin: this adapter may reach ONLY the official GDELT API host.
_GDELT_HOST = "api.gdeltproject.org"
ALLOWED_HOSTS = frozenset({_GDELT_HOST})
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5MB cap — 50 ArtList records fit easily
GDELT_TIMEOUT_S = 30  # GDELT is slow; generous but explicit (A8 #3)

_GDELT_DOC_URL = f"https://{_GDELT_HOST}/api/v2/doc/doc"
GDELT_TIMESPAN = "7d"  # matches signals/news.py WINDOW_DAYS
GDELT_MAX_RECORDS = 50

# A8 config-validation bar (#9 curated-config precedent): a query phrase
# shapes the outbound GDELT URL, so it is validated at LOAD time and
# re-checked at build time — a malformed phrase never reaches egress.
_PHRASE_RE = re.compile(r"^[A-Za-z0-9 .&'\-]{2,60}$")

# Sanitization bounds (contract v1.2.8 §4a #3/#4).
MAX_HEADLINE_LEN = 500
MAX_SOURCE_NAME_LEN = 200
MAX_URL_LEN = 2048

_WS_RE = re.compile(r"\s+")
_SEENDATE_FORMAT = "%Y%m%dT%H%M%SZ"  # GDELT seendate: YYYYMMDDTHHMMSSZ (UTC)


def sanitize_text(value: Any, *, max_len: int = MAX_HEADLINE_LEN) -> str:
    """Sanitize ON INGEST — before the DB store AND before VADER input:
    collapse whitespace runs to a single space, strip control/format chars
    (Unicode Cc/Cf, incl. NULs), NFC-normalize, strip, cap length. HTML tags
    survive as literal text (React escapes at render — never raw-inject)."""
    text = _WS_RE.sub(" ", str(value))
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    return unicodedata.normalize("NFC", text).strip()[:max_len]


def validate_url(value: Any) -> str | None:
    """Item-level URL gate: http/https scheme only, capped length, no
    control chars. Anything else -> None and the WHOLE item is dropped —
    a javascript:/data: URL is adversarial, and a NULL url would defeat the
    UNIQUE(ticker_id, url, published_at) dedupe key."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > MAX_URL_LEN:
        return None
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in url):
        return None
    if urllib.parse.urlparse(url).scheme.lower() not in ("http", "https"):
        return None
    return url


def parse_seendate(value: Any) -> datetime | None:
    """GDELT seendate 'YYYYMMDDTHHMMSSZ' -> aware UTC datetime; unparseable
    -> None (item dropped — a headline without a real timestamp cannot be
    windowed honestly)."""
    try:
        return datetime.strptime(str(value), _SEENDATE_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def load_news_queries(path: str | Path) -> dict[str, tuple[str, ...]]:
    """Load + validate the curated ticker -> query-phrases map (the #9
    curated-config precedent; NO fuzzy name matching). Invalid phrases are
    REJECTED at load (A8) — before any egress could be shaped by them.
    Keys starting with '_' are comments."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    queries: dict[str, tuple[str, ...]] = {}
    for symbol, phrases in data.items():
        if symbol.startswith("_"):
            continue  # e.g. "_comment"
        validated = []
        for phrase in phrases:
            if not _PHRASE_RE.fullmatch(str(phrase)):
                raise ValueError(
                    f"news queries config: invalid query phrase {phrase!r} for {symbol}"
                )
            validated.append(str(phrase))
        if not validated:
            raise ValueError(f"news queries config: no phrases for {symbol}")
        queries[str(symbol)] = tuple(validated)
    return queries


def build_query(full_symbol: str, queries: dict[str, tuple[str, ...]]) -> str:
    """Build the GDELT query string for one ticker. Curated phrases are
    quoted and OR-joined in parentheses; unlisted tickers fall back to the
    exact quoted symbol. ` sourcelang:eng` is always appended (VADER is an
    English lexicon). Phrases are re-checked against the allowlist regex
    here (defense in depth behind the loader) — never egress a bad phrase."""
    phrases = queries.get(full_symbol)
    if phrases:
        for phrase in phrases:
            if not _PHRASE_RE.fullmatch(phrase):
                raise ValueError(f"query phrase rejected by egress allowlist: {phrase!r}")
        quoted = " OR ".join(f'"{p}"' for p in phrases)
        term = f"({quoted})" if len(phrases) > 1 else quoted
    else:
        # A8 #1 / Y-1: fullmatch allowlist before any egress — the symbol
        # shapes the outbound GDELT request.
        check_symbol(full_symbol)
        term = f'"{full_symbol}"'
    return f"{term} sourcelang:eng"


class GdeltClient(Protocol):
    def fetch_articles(self, full_symbol: str, query: str) -> list[dict[str, Any]]:
        """Return raw GDELT ArtList article dicts
        [{title, url, seendate, domain}, ...] for the built query."""
        ...


class RealGdeltClient:
    """The only GDELT-touching code, via the hardened common.http_get (HTTPS
    only, host pinned to api.gdeltproject.org, redirects refused outside the
    allowlist, 30s timeout, size cap). Response handling per §4a:
    - HTTP 200 empty body OR JSON object lacking "articles" -> legitimately
      0 results (fetch OK — "no news is neutral news" downstream);
    - non-empty body that is not parseable JSON (e.g. an HTML error page) ->
      this ticker's query FAILED (raise; never trust error pages as data)."""

    def fetch_articles(self, full_symbol: str, query: str) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({
            "query": query, "mode": "ArtList", "format": "json",
            "maxrecords": GDELT_MAX_RECORDS, "timespan": GDELT_TIMESPAN,
            "sort": "DateDesc",
        })
        body = http_get(f"{_GDELT_DOC_URL}?{params}", allowed_hosts=ALLOWED_HOSTS,
                        max_bytes=MAX_RESPONSE_BYTES, timeout=GDELT_TIMEOUT_S)
        if not body.strip():
            return []  # 200 + empty body: legitimate 0 results
        try:
            payload = json.loads(body)
        except ValueError as exc:
            # A8 #6 log hygiene: never echo the body — it may be an HTML page.
            raise ValueError("GDELT response is not parseable JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("GDELT response has unexpected JSON shape")
        articles = payload.get("articles")
        if articles is None:
            return []  # JSON object without "articles": legitimate 0 results
        return list(articles)


class FixtureGdeltClient:
    """Deterministic fixture mode (FR-19): reproducible synthetic articles,
    zero network. 2330.TW -> 2 articles (positive-ish + neutral), AAPL -> 1,
    6488.TWO -> 0 (exercises the fetch-ok-but-empty neutral path live), any
    other symbol -> 0. seendates sit inside the 7d window relative to the
    run day, so re-runs on one day are byte-stable."""

    def fetch_articles(self, full_symbol: str, query: str) -> list[dict[str, Any]]:
        today = datetime.now(UTC).strftime("%Y%m%d")
        fixtures = {
            "2330.TW": [
                {"title": "TSMC posts record quarterly profit on strong AI chip demand",
                 "url": "https://news.example.com/tsmc-record-profit",
                 "seendate": f"{today}T083000Z", "domain": "news.example.com"},
                {"title": "Taiwan Semiconductor schedules its next earnings call",
                 "url": "https://wire.example.org/tsmc-earnings-call",
                 "seendate": f"{today}T111500Z", "domain": "wire.example.org"},
            ],
            "AAPL": [
                {"title": "Apple Inc unveils updated device lineup",
                 "url": "https://news.example.com/apple-lineup",
                 "seendate": f"{today}T093000Z", "domain": "news.example.com"},
            ],
        }
        return fixtures.get(full_symbol, [])


@dataclass
class NewsIngestStats:
    tickers_ok: int = 0        # fetch ok, >=1 article kept
    tickers_empty: int = 0     # fetch ok, 0 kept articles (neutral downstream)
    headlines_ingested: int = 0
    items_rejected: int = 0    # sanitize/URL/date/sentiment rejects — counted
    failed_symbols: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Run message for pipeline_run. When tickers failed this ENDS with
        the machine-stable token `failed_tickers=SYM1,SYM2` (comma-joined, no
        spaces) — parsed by signals/news.news_signal (see module docstring)."""
        msg = (f"tickers ok={self.tickers_ok} empty={self.tickers_empty} "
               f"failed={len(self.failed_symbols)}; "
               f"headlines ingested={self.headlines_ingested} "
               f"rejected={self.items_rejected}")
        if self.failed_symbols:
            # D-1 token integrity (fail-closed totality): only SYMBOL_RE-
            # conforming symbols may enter the machine-parsed token — one
            # malformed symbol must never truncate conforming neighbours out
            # of it (the regex stops at whitespace). The failed= count above
            # stays the FULL count and dropped symbols are logged, so evidence
            # is never silently lost; the read side (signals/news.py)
            # independently fail-closes any non-conforming symbol.
            safe = [s for s in self.failed_symbols if SYMBOL_RE.fullmatch(s)]
            if len(safe) < len(self.failed_symbols):
                logger.warning(
                    "gdelt run message: %d non-token-safe failed symbol(s) "
                    "omitted from failed_tickers=",
                    len(self.failed_symbols) - len(safe))
            if safe:
                msg += f"; failed_tickers={','.join(safe)}"
        return msg


# Headlines are immutable observations: DO NOTHING on conflict — a re-run
# never duplicates and never rewrites history (idempotency key = the full
# UNIQUE(ticker_id, url, published_at) constraint).
_INSERT_NEWS = """
    INSERT INTO news_item (ticker_id, published_at, headline, url, source_name,
                           sentiment, score)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (ticker_id, url, published_at) DO NOTHING
"""


def _make_analyzer() -> Any:
    """Lazy import (yfinance precedent): vaderSentiment loads its lexicon
    from the package — no network."""
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    return SentimentIntensityAnalyzer()


def _prepare_item(article: dict[str, Any], analyzer: Any) -> tuple | None:
    """Sanitize/validate/score ONE article -> bind tuple, or None (rejected).
    Order matters: sanitize FIRST so both the DB and VADER only ever see the
    cleaned, length-capped headline (contract v1.2.8 §4a #3/#5)."""
    headline = sanitize_text(article.get("title"))
    if not headline:
        return None  # empty after sanitization: drop the item
    url = validate_url(article.get("url"))
    if url is None:
        return None  # bad scheme/oversized: drop the WHOLE item
    published_at = parse_seendate(article.get("seendate"))
    if published_at is None:
        return None  # unparseable seendate: drop the item
    try:
        compound = Decimal(repr(float(analyzer.polarity_scores(headline)["compound"])))
    except (KeyError, TypeError, ValueError):
        return None
    if not (Decimal(-1) <= compound <= Decimal(1)):
        return None  # out-of-range compound: REJECT the row, never clamp
    sentiment = compound.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    # Item-level score column: 2x compound clamped to [-2,2] at 2dp —
    # informational only; the module signal is computed in signals/news.py.
    score = max(Decimal(-2), min(Decimal(2), compound * 2))
    score = score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    source_name = sanitize_text(article.get("domain", ""),
                                max_len=MAX_SOURCE_NAME_LEN) or None
    return (published_at, headline, url, source_name, sentiment, score)


async def ingest_gdelt(
    conn: Any,
    client: GdeltClient,
    *,
    queries: dict[str, tuple[str, ...]] | None = None,
    sleeper=asyncio.sleep,
    analyzer: Any = None,
    only_ticker: str | None = None,
) -> NewsIngestStats:
    """Ingest 7d headlines for all covered tickers. Raises AdapterUnavailable
    only when NOTHING succeeded (an empty-but-fetched ticker IS a success);
    per-ticker failures are reported in stats + the failed_tickers= token.
    Task #20 (ADR-009): only_ticker narrows the covered-ticker query to one
    full_symbol for an on-demand run; None (the daily batch) is unchanged."""
    if queries is None:
        from app.core.config import get_settings

        queries = load_news_queries(get_settings().news_queries_path)
    stats = NewsIngestStats()
    analyzer = analyzer or _make_analyzer()

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
        if i > 0:
            await sleeper(PACING_DELAY_S)  # paced egress between tickers (A8 #3)
        try:
            await _ingest_one(conn, client, t["id"], t["full_symbol"], queries,
                              analyzer, stats, sleeper=sleeper)
        except AdapterUnavailable:
            raise  # source-wide failure signalled by the client: propagate
        except Exception as exc:  # per-ticker isolation (§22.4)
            stats.failed_symbols.append(t["full_symbol"])
            # A8 #6 log hygiene: ticker+status only — never bodies/headlines.
            logger.warning("gdelt ingest failed for %s: %s", t["full_symbol"], exc)

    if stats.tickers_ok + stats.tickers_empty == 0:
        raise AdapterUnavailable(
            f"all tickers failed: failed_tickers={','.join(stats.failed_symbols)}"
        )
    return stats


async def _ingest_one(
    conn: Any,
    client: GdeltClient,
    ticker_id: int,
    full_symbol: str,
    queries: dict[str, tuple[str, ...]],
    analyzer: Any,
    stats: NewsIngestStats,
    sleeper=asyncio.sleep,
) -> None:
    # Phrase/symbol allowlist runs INSIDE build_query, before any egress.
    query = build_query(full_symbol, queries)
    articles = await _with_retries(client.fetch_articles, full_symbol, query,
                                   sleeper=sleeper)
    rows = []
    for article in articles:
        row = _prepare_item(article, analyzer)
        if row is None:
            stats.items_rejected += 1  # rejected AND counted — never silent
            continue
        rows.append((ticker_id, *row))
    if not rows:
        # Fetch succeeded, zero kept headlines — a legitimate quiet week
        # (contract v1.2.8 §4a: downstream reads this as neutral, NOT missing).
        stats.tickers_empty += 1
        return
    await conn.executemany(_INSERT_NEWS, rows)
    stats.headlines_ingested += len(rows)
    stats.tickers_ok += 1
