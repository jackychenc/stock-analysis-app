"""SEC EDGAR 13F ingestion adapter — roadmap Step 3 (task #9, US half).

Writes QUARTERLY institutional positioning -> institutional_position_us for
covered US tickers, from a CURATED set of major 13F filers (config/
curated_13f.json — PM condition: tiny polite egress, not an EDGAR crawl).
13F filings are quarterly AND ~45 days delayed — this is "quarterly
positioning", never live flow (R-04 / FR-16 framing).

Scope guardrails:
- ingestion ONLY: `score` stays NULL (chip signal calculator = task #10);
- per-FILER isolation (the unit of fetch is a filer, not a ticker): one
  filer's failure never aborts the others (§22.4);
- idempotent upserts on the 3-part key (ticker_id, quarter, filer_name) —
  omitting filer_name would collapse multiple filers into one row (T9-B1);
- a US ticker with no CUSIP mapping / no curated holdings gets ZERO rows —
  honest unavailable for task #10, never fabricated;
- XML is parsed with defusedxml ONLY (A8: XXE/billion-laughs on hostile-ish
  upstream data) — stdlib xml.etree is banned on this path;
- filer_name is a KEY component: trimmed, control-chars stripped, capped.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from app.batch.adapters.common import (
    AdapterUnavailable,
    _int,
    _num,
    _with_retries,
    http_get,
)

logger = logging.getLogger(__name__)

# A8 #2 egress pin: this adapter may reach ONLY official SEC hosts.
ALLOWED_HOSTS = frozenset({"data.sec.gov", "www.sec.gov", "efts.sec.gov"})
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10MB cap — large filers file big tables

# SEC fair-access policy: declared descriptive User-Agent or EDGAR blocks
# (A6 co-verify item), and <=10 req/s — we pace at ~0.15s per request.
USER_AGENT = "stock-analysis-app/0.1 (personal decision-support; contact: admin@localhost)"
EDGAR_PACING_DELAY_S = 0.15

# A8 config-validation bar: a CIK shapes the outbound EDGAR URL, so it is
# validated at LOAD time — a malformed config entry never reaches egress.
_CIK_RE = re.compile(r"^\d{10}$")
_CUSIP_RE = re.compile(r"^[0-9A-Z]{9}$")
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_FILER_NAME_LEN = 200

# A8 #4 bounds: no filer holds more than 1e13 shares / $10T of one issue.
_MAX_SHARES = 10_000_000_000_000
_MAX_VALUE = Decimal("10000000000000")


def sanitize_filer_name(name: Any) -> str:
    """filer_name lands in a UNIQUE key: trim, strip control chars/newlines,
    cap length — a hostile name must not smuggle log/SQL-adjacent garbage."""
    return _CTRL_CHARS_RE.sub("", str(name)).strip()[:_MAX_FILER_NAME_LEN]


@dataclass(frozen=True)
class Filer:
    cik: str
    name: str


@dataclass(frozen=True)
class Curated13F:
    filers: tuple[Filer, ...]
    cusip_map: dict[str, str]  # full_symbol -> CUSIP


def load_curated_13f(path: str | Path) -> Curated13F:
    """Load + validate the curated config. Invalid entries are REJECTED at
    load (A8) — before any egress could be shaped by them."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    filers = []
    for entry in data.get("filers", []):
        cik = str(entry.get("cik", ""))
        if not _CIK_RE.fullmatch(cik):
            raise ValueError(f"curated 13F config: invalid CIK {cik!r} (must be 10 digits)")
        name = sanitize_filer_name(entry.get("name", ""))
        if not name:
            raise ValueError(f"curated 13F config: empty filer name for CIK {cik}")
        filers.append(Filer(cik=cik, name=name))
    if not filers:
        raise ValueError("curated 13F config: no filers configured")
    cusip_map = {}
    for symbol, cusip in (data.get("cusip_map") or {}).items():
        if not _CUSIP_RE.fullmatch(str(cusip)):
            raise ValueError(f"curated 13F config: invalid CUSIP {cusip!r} for {symbol}")
        cusip_map[str(symbol)] = str(cusip)
    return Curated13F(filers=tuple(filers), cusip_map=cusip_map)


class EdgarClient(Protocol):
    def fetch_latest_13f(self, cik: str) -> dict[str, Any]:
        """Return {filer_name, quarter (date, periodOfReport),
        holdings: [{cusip, shares, value}, ...]} for the newest 13F-HR."""
        ...


def parse_13f_info_table(xml_bytes: bytes) -> list[dict[str, Any]]:
    """Pure parse of a 13F information-table XML -> [{cusip, shares, value}].

    defusedxml ONLY (A8 XXE requirement): DOCTYPE/external-entity payloads
    raise instead of resolving — never stdlib xml.etree on upstream data.
    Namespace-agnostic tag matching (filers vary their ns prefixes)."""
    from defusedxml import ElementTree as SafeET

    root = SafeET.fromstring(xml_bytes)

    def local(tag: str) -> str:
        return tag.rpartition("}")[2]

    def find_text(el, name: str) -> str | None:
        for child in el.iter():
            if local(child.tag) == name and child.text is not None:
                return child.text.strip()
        return None

    holdings = []
    for el in root.iter():
        if local(el.tag) != "infoTable":
            continue
        holdings.append({
            "cusip": (find_text(el, "cusip") or "").upper(),
            "shares": find_text(el, "sshPrnamt"),
            "value": find_text(el, "value"),
        })
    return holdings


class RealEdgarClient:
    """The only EDGAR-touching code: submissions index -> newest 13F-HR ->
    filing index -> information-table XML. Every request carries the declared
    User-Agent and is paced at ~0.15s (SEC fair-access, 10 req/s courtesy)."""

    def __init__(self):
        self._last_request_ts = 0.0

    def _get(self, url: str) -> bytes:
        # Client-internal pacing between consecutive EDGAR requests.
        wait = EDGAR_PACING_DELAY_S - (time.monotonic() - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()
        return http_get(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=MAX_RESPONSE_BYTES,
                        headers={"User-Agent": USER_AGENT})

    def fetch_latest_13f(self, cik: str) -> dict[str, Any]:
        if not _CIK_RE.fullmatch(cik):  # defense in depth behind the loader
            raise ValueError("CIK rejected by egress allowlist")
        subs = json.loads(self._get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
        recent = subs.get("filings", {}).get("recent", {})
        accession, period = None, None
        for i, form in enumerate(recent.get("form", [])):
            if form == "13F-HR":  # newest first in EDGAR's recent list
                accession = recent["accessionNumber"][i]
                period = recent["reportDate"][i]
                break
        if not accession or not period:
            raise ValueError("no 13F-HR filing found")
        quarter = date.fromisoformat(period)

        acc_nodash = accession.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}"
        index = json.loads(self._get(f"{base}/index.json"))
        xml_name = self._pick_info_table(index)
        holdings = parse_13f_info_table(self._get(f"{base}/{xml_name}"))
        return {
            "filer_name": subs.get("name", ""),
            "quarter": quarter,
            "holdings": holdings,
        }

    @staticmethod
    def _pick_info_table(index: dict[str, Any]) -> str:
        """The information table is the non-primary .xml in the filing dir;
        prefer names containing 'infotable' (the EDGAR convention)."""
        names = [item.get("name", "") for item in index.get("directory", {}).get("item", [])]
        xmls = [n for n in names if n.lower().endswith(".xml")
                and "primary_doc" not in n.lower()]
        for n in xmls:
            if "infotable" in n.lower():
                return n
        if xmls:
            return xmls[0]
        raise ValueError("no information-table XML in filing index")


def _latest_quarter_end(today: date) -> date:
    """Most recent completed calendar quarter end (13F reporting period)."""
    q_ends = [date(today.year - 1, 12, 31), date(today.year, 3, 31),
              date(today.year, 6, 30), date(today.year, 9, 30),
              date(today.year, 12, 31)]
    return max(d for d in q_ends if d < today)


class FixtureEdgarClient:
    """Deterministic fixture mode (FR-19 / T9-D1): synthetic filings for the
    curated filers, zero network. Holdings cover every mapped CUSIP so a
    covered ticker (e.g. AAPL) gets multi-filer rows (T9-S6). Values derive
    only from (cik, cusip, quarter) — byte-stable re-runs."""

    def __init__(self, curated: Curated13F):
        self._curated = curated

    def fetch_latest_13f(self, cik: str) -> dict[str, Any]:
        filer = next((f for f in self._curated.filers if f.cik == cik), None)
        if filer is None:
            raise ValueError("unknown CIK for fixture")
        quarter = _latest_quarter_end(date.today())
        seed = int(cik) + quarter.toordinal()
        holdings = []
        for cusip in sorted(self._curated.cusip_map.values()):
            h_seed = seed + sum(ord(c) for c in cusip)
            shares = 1_000_000 + h_seed % 9_000_000
            holdings.append({
                "cusip": cusip,
                "shares": shares,
                "value": shares * (50 + h_seed % 400),  # 13F value in USD
            })
        return {"filer_name": filer.name, "quarter": quarter, "holdings": holdings}


@dataclass
class EdgarIngestStats:
    filers_ok: int = 0
    filers_failed: int = 0
    rows_upserted: int = 0
    rows_skipped: int = 0        # invalid holdings — counted, not hidden
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        msg = (f"filers ok={self.filers_ok} failed={self.filers_failed}; "
               f"rows upserted={self.rows_upserted} skipped={self.rows_skipped} "
               f"(quarterly positioning, R-04)")
        if self.failures:
            msg += f"; failures: {'; '.join(self.failures)}"
        return msg


# v1.2.5 provenance: ingested_at refreshed on UPDATE. The 3-part conflict key
# is load-bearing (T9-B1): filer_name distinguishes multi-filer rows.
_UPSERT_POSITION = """
    INSERT INTO institutional_position_us
        (ticker_id, quarter, filer_name, shares, market_value, source)
    VALUES ($1, $2, $3, $4, $5, 'edgar_13f')
    ON CONFLICT (ticker_id, quarter, filer_name) DO UPDATE SET
        shares = EXCLUDED.shares, market_value = EXCLUDED.market_value,
        source = EXCLUDED.source, ingested_at = now()
"""


async def ingest_edgar_13f(
    conn: Any,
    client: EdgarClient,
    *,
    asof: date | None = None,
    sleeper=asyncio.sleep,
    curated: Curated13F | None = None,
) -> EdgarIngestStats:
    """Ingest latest 13F positions for curated filers x covered US tickers.
    Raises AdapterUnavailable only when NO filer succeeded; partial failures
    are reported in stats (honest, not fatal)."""
    if curated is None:
        from app.core.config import get_settings

        curated = load_curated_13f(get_settings().curated_13f_path)
    stats = EdgarIngestStats()

    # T9-S3 market routing: US tickers only — TW tickers never reach EDGAR.
    tickers = await conn.fetch(
        "SELECT id, full_symbol FROM ticker "
        "WHERE is_covered AND exchange = 'US' ORDER BY id"
    )
    if not tickers:
        raise AdapterUnavailable("no covered US tickers to ingest")

    # Reverse map cusip -> ticker_id, restricted to covered tickers. A covered
    # ticker absent from cusip_map simply gets no rows (honest unavailable).
    cusip_index = {
        curated.cusip_map[t["full_symbol"]]: t["id"]
        for t in tickers if t["full_symbol"] in curated.cusip_map
    }
    if not cusip_index:
        raise AdapterUnavailable("no covered US ticker has a CUSIP mapping")

    for i, filer in enumerate(curated.filers):
        if i > 0:
            await sleeper(EDGAR_PACING_DELAY_S)  # EDGAR courtesy pacing (A8 #3)
        try:
            await _ingest_filer(conn, client, filer, cusip_index, stats, sleeper=sleeper)
            stats.filers_ok += 1
        except Exception as exc:  # per-filer isolation (§22.4)
            stats.filers_failed += 1
            stats.failures.append(f"{filer.name}: {exc}")
            # A8 #6 log hygiene: source+filer+status only, never bodies.
            logger.warning("edgar_13f ingest failed for CIK %s: %s", filer.cik, exc)

    if stats.filers_ok == 0:
        raise AdapterUnavailable(f"all filers failed: {'; '.join(stats.failures)}")
    return stats


def _valid_position(shares: int | None, value: Decimal | None) -> bool:
    """A8 #4 / T9-M2: 13F holdings can NEVER be negative (unlike TW nets);
    reject negatives and absurd magnitudes. A null value alone is tolerable
    (persisted NULL, T9-M1) but a holding needs at least shares."""
    if shares is None or shares < 0 or shares > _MAX_SHARES:
        return False
    if value is not None and (value < 0 or value > _MAX_VALUE):
        return False
    return True


async def _ingest_filer(
    conn: Any,
    client: EdgarClient,
    filer: Filer,
    cusip_index: dict[str, int],
    stats: EdgarIngestStats,
    sleeper=asyncio.sleep,
) -> None:
    # A8 #1: validated at load, re-checked before egress (defense in depth).
    if not _CIK_RE.fullmatch(filer.cik):
        raise ValueError("CIK rejected by egress allowlist")

    filing = await _with_retries(client.fetch_latest_13f, filer.cik, sleeper=sleeper)
    quarter = filing.get("quarter")
    if not isinstance(quarter, date):
        raise ValueError("13F filing missing periodOfReport quarter")
    # Final ruling (Cindy 2026-07-08): the filer_name KEY column stores the
    # 10-digit zero-padded CIK — a stable key immune to re-spelled institution
    # names. Display names live in the curated config (and logs) only; a
    # future readable column would be a v1.2.6 schema delta, not #9.
    filer_key = filer.cik

    rows = []
    for holding in filing.get("holdings", []):
        ticker_id = cusip_index.get(str(holding.get("cusip", "")).upper())
        if ticker_id is None:
            continue  # not a covered/mapped ticker — silently out of scope
        shares = _int(holding.get("shares"))
        value = _num(holding.get("value"))
        if not _valid_position(shares, value):
            stats.rows_skipped += 1  # rejected AND counted (T9-M2/M3)
            continue
        rows.append((ticker_id, quarter, filer_key, shares, value))
    # A filer legitimately may hold none of our covered tickers: zero rows is
    # NOT a failure — task #10 reads the absence as chip-unavailable.
    if rows:
        await conn.executemany(_UPSERT_POSITION, rows)
        stats.rows_upserted += len(rows)
