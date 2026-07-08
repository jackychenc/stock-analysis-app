"""TWSE/TPEx chip ingestion adapter — roadmap Step 3 (task #9, TW half).

Writes daily three-institution chip facts -> chip_data_tw for every covered
TW ticker (exchange TWSE/TPEx only — market routing per T9-S3, US tickers
never touch this source). Scope guardrails:
- ingestion ONLY: `score` stays NULL (chip signal calculator = task #10);
- per-ticker isolation: one ticker's failure never aborts the others (§22.4);
- idempotent upserts on (ticker_id, trade_date); provenance source +
  ingested_at refreshed on update (v1.2.5);

METHODOLOGY NOTE (A3 no-double-count invariant, 2026-07-08) — source-column
-> schema-column mapping; the three nets partition the institutions with each
counted exactly once:
- foreign_net        = Foreign & Mainland investors, foreign-dealers-EXCLUDED
                       (TWSE 外陸資買賣超股數(不含外資自營商); TPEx
                       "...(Foreign Dealers excluded)-Difference").
- dealer_net         = dealers INCLUDING foreign dealers (TWSE 自營商買賣超
                       + 外資自營商買賣超; TPEx Dealers-Difference + the
                       foreign-dealer component (incl − excl)). If the
                       foreign-dealer column is absent upstream, dealer_net
                       falls back to the dealers total and the component is
                       dropped — visible here, never silently blended.
- investment_trust_net = investment trust -Difference (投信買賣超股數).
Task #10's chip signal must interpret the nets under this convention.
- field validity is PER-FIELD (T9-M2): the three *_net columns are SIGNED —
  net-sell is real and stored honestly; margin_balance/block_trade_volume can
  never be negative; NaN/inf/absurd magnitudes are rejected AND counted.

Known limitation (documented, not hidden): latest-trading-day fetch only —
chip history accumulates run by run (daily batch); no deep backfill. The
official T86/OpenAPI daily endpoints serve one day per call, and hammering
them for history would violate the R-01 politeness bar.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from app.batch.adapters.common import (
    PACING_DELAY_S,
    AdapterUnavailable,
    _int,
    _with_retries,
    check_symbol,
    http_get,
)

logger = logging.getLogger(__name__)

# A8 #2 egress pin: this adapter may reach ONLY official TWSE/TPEx hosts.
ALLOWED_HOSTS = frozenset({"www.twse.com.tw", "openapi.twse.com.tw", "www.tpex.org.tw"})
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5MB cap — whole-market daily JSON fits

_TWSE_T86_URL = ("https://www.twse.com.tw/rwd/zh/fund/T86"
                 "?date={yyyymmdd}&selectType=ALLBUT0999&response=json")
_TPEX_3INSTI_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"

# A8 #4: bounds beyond NaN — poisoned upstream values must not land in the DB.
_MAX_CHIP_MAGNITUDE = 10_000_000_000_000  # 1e13 shares/lots is not a real market

# T9-M2 per-field spec: nets are SIGNED (net sell is legitimate);
# balances/volumes cannot be negative.
_SIGNED_FIELDS = ("foreign_net", "investment_trust_net", "dealer_net")
_UNSIGNED_FIELDS = ("margin_balance", "block_trade_volume")


class TwseTpexClient(Protocol):
    def fetch_daily_chip(self, symbol: str, exchange: str) -> list[dict[str, Any]]:
        """Return [{trade_date, foreign_net, investment_trust_net, dealer_net,
        margin_balance, block_trade_volume}, ...] for the latest trading day.
        `symbol` is the bare TW code (e.g. '2330'); ints may be None."""
        ...


def _parse_tw_int(value: Any) -> int | None:
    """TWSE numbers arrive as '1,234,567' strings (possibly negative)."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if value in ("", "--"):
            return None
    return _int(value)


def _roc_to_gregorian(value: str) -> date | None:
    """TPEx (and some TWSE surfaces) report ROC/Minguo dates: '1150707' =
    ROC year 115 -> 2026-07-07 (A7 live pre-flight — parsing as ISO would
    silently corrupt trade_date). Accepts 6-7 digit ROC or 8-digit Gregorian."""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    try:
        if len(digits) == 8:  # already Gregorian YYYYMMDD
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        if len(digits) in (6, 7):  # ROC: [Y]YYMMDD
            return date(int(digits[:-4]) + 1911, int(digits[-4:-2]), int(digits[-2:]))
    except ValueError:
        return None
    return None


def _normalize_key(key: str) -> str:
    """TPEx field names have irregular whitespace (leading spaces, spaces
    before dashes, mid-word spaces) and near-duplicate keys — strip ALL
    whitespace and casefold before matching (A7 live pre-flight gotcha #2)."""
    return "".join(str(key).split()).casefold()


def _latest_weekday(today: date) -> date:
    d = today
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


class RealTwseTpexClient:
    """The only TWSE/TPEx-touching code. Both official endpoints return the
    WHOLE market for one day, so responses are cached per (exchange, date) —
    N covered tickers cost 1 request per exchange, not N (R-01 politeness).

    Column/field names are located by header lookup (TWSE 'fields' array /
    TPEx object keys), never by hard-coded index — upstream reorders happen.
    margin_balance/block_trade_volume come from different endpoints not yet
    wired; the real client returns them as None (honest NULL, T9-M1)."""

    def __init__(self, asof: date | None = None):
        self._asof = _latest_weekday(asof or date.today())
        self._cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._response_date: dict[str, date | None] = {}

    def fetch_daily_chip(self, symbol: str, exchange: str) -> list[dict[str, Any]]:
        table = self._market_table(exchange)
        nets = table.get(symbol)
        if nets is None:
            return []  # no 3-institution activity reported for this code today
        # trade_date comes from the RESPONSE (ROC->Gregorian), never the local
        # clock — a holiday/publication-lag day would otherwise mislabel rows.
        row_date = nets.pop("trade_date", None) or self._response_date.get(exchange)
        if row_date is None:
            raise ValueError(f"{exchange} response carried no parseable trade date")
        return [{
            "trade_date": row_date,
            **nets,
            "margin_balance": None,      # separate endpoint — known limitation
            "block_trade_volume": None,  # separate endpoint — known limitation
        }]

    def _market_table(self, exchange: str) -> dict[str, dict[str, int | None]]:
        if exchange not in self._cache:
            fetch = self._fetch_twse if exchange == "TWSE" else self._fetch_tpex
            self._cache[exchange] = fetch()
        return self._cache[exchange]

    def _fetch_twse(self) -> dict[str, dict[str, int | None]]:
        url = _TWSE_T86_URL.format(yyyymmdd=self._asof.strftime("%Y%m%d"))
        payload = json.loads(http_get(url, allowed_hosts=ALLOWED_HOSTS,
                                      max_bytes=MAX_RESPONSE_BYTES))
        if payload.get("stat") != "OK":
            raise ValueError(f"TWSE T86 returned stat={payload.get('stat')!r}")
        # Response-declared date (Gregorian or ROC) is authoritative for rows.
        self._response_date["TWSE"] = _roc_to_gregorian(payload.get("date", ""))
        fields = payload.get("fields") or []

        def col(*names: str) -> int:
            for i, f in enumerate(fields):
                if f in names:
                    return i
            raise ValueError(f"TWSE T86 missing expected column {names[0]}")

        def col_opt(*names: str) -> int | None:
            for i, f in enumerate(fields):
                if f in names:
                    return i
            return None

        i_code = col("證券代號")
        i_foreign = col("外陸資買賣超股數(不含外資自營商)", "外資買賣超股數")
        i_trust = col("投信買賣超股數")
        i_dealer = col("自營商買賣超股數")
        # A3 no-double-count invariant: foreign_net EXCLUDES foreign dealers,
        # so dealer_net must INCLUDE them (外資自營商 counted exactly once).
        i_fdealer = col_opt("外資自營商買賣超股數")
        table: dict[str, dict[str, Any]] = {}
        for row in payload.get("data") or []:
            dealer = _parse_tw_int(row[i_dealer])
            if i_fdealer is not None:
                fdealer = _parse_tw_int(row[i_fdealer])
                if dealer is not None and fdealer is not None:
                    dealer += fdealer
            table[str(row[i_code]).strip()] = {
                "foreign_net": _parse_tw_int(row[i_foreign]),
                "investment_trust_net": _parse_tw_int(row[i_trust]),
                "dealer_net": dealer,
            }
        return table

    # Normalized TPEx field names per A7's live schema dump (2026-07-08).
    # foreign_net convention LOCKED by Cindy: "include Mainland Area Investors
    # (Foreign Dealers excluded)" — avoids double-counting dealer_net.
    _TPEX_FOREIGN_KEYS = (
        "foreigninvestorsincludemainlandareainvestors(foreigndealersexcluded)-difference",
        "foreigninvestorsincludemainlandareainvestors-difference",
    )
    _TPEX_TRUST_KEY = "securitiesinvestmenttrustcompanies-difference"
    _TPEX_DEALER_KEY = "dealers-difference"
    _TPEX_CODE_KEY = "securitiescompanycode"
    _TPEX_DATE_KEY = "date"

    def _fetch_tpex(self) -> dict[str, dict[str, Any]]:
        payload = json.loads(http_get(_TPEX_3INSTI_URL, allowed_hosts=ALLOWED_HOSTS,
                                      max_bytes=MAX_RESPONSE_BYTES))

        table: dict[str, dict[str, Any]] = {}
        for row in payload:
            norm = {_normalize_key(k): v for k, v in row.items()}
            code = norm.get(self._TPEX_CODE_KEY)
            if code is None:
                continue
            foreign_excl = _parse_tw_int(norm.get(self._TPEX_FOREIGN_KEYS[0]))
            foreign_incl = _parse_tw_int(norm.get(self._TPEX_FOREIGN_KEYS[1]))
            foreign = foreign_excl if foreign_excl is not None else foreign_incl
            dealer = _parse_tw_int(norm.get(self._TPEX_DEALER_KEY))
            # A3 invariant: fold the foreign-dealer component (incl − excl)
            # into dealer_net so 外資自營商 is counted exactly once.
            if (dealer is not None and foreign_excl is not None
                    and foreign_incl is not None):
                dealer += foreign_incl - foreign_excl
            table[str(code).strip()] = {
                "trade_date": _roc_to_gregorian(norm.get(self._TPEX_DATE_KEY, "")),
                "foreign_net": foreign,
                "investment_trust_net": _parse_tw_int(norm.get(self._TPEX_TRUST_KEY)),
                "dealer_net": dealer,
            }
        return table


class FixtureTwseTpexClient:
    """Deterministic fixture mode (FR-19 / T9-D1): reproducible synthetic chip
    rows, zero network. Values derive only from (symbol, date), so re-runs are
    byte-stable. Some symbols deliberately produce NEGATIVE nets — net-sell is
    a legitimate market fact the pipeline must store honestly (T9-M2)."""

    def fetch_daily_chip(self, symbol: str, exchange: str) -> list[dict[str, Any]]:
        d = _latest_weekday(date.today())
        seed = sum(ord(c) for c in symbol) + d.toordinal()
        sign = -1 if seed % 3 == 0 else 1  # every 3rd seed: foreign net-SELL day
        return [{
            "trade_date": d,
            "foreign_net": sign * (1_000_000 + seed % 500_000),
            "investment_trust_net": (seed % 200_000) - 100_000,  # signed wobble
            "dealer_net": (seed % 150_000) - 75_000,
            "margin_balance": 10_000_000 + seed % 5_000_000,
            "block_trade_volume": seed % 1_000_000,
        }]


@dataclass
class ChipIngestStats:
    tickers_ok: int = 0
    tickers_failed: int = 0
    rows_upserted: int = 0
    rows_skipped: int = 0        # invalid per-field values — counted, not hidden
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        msg = (f"tickers ok={self.tickers_ok} failed={self.tickers_failed}; "
               f"rows upserted={self.rows_upserted} skipped={self.rows_skipped}")
        if self.failures:
            msg += f"; failures: {'; '.join(self.failures)}"
        return msg


# v1.2.5 provenance: ingested_at defaults to now() on INSERT and is explicitly
# refreshed on UPDATE (last-fetched semantics). `score` is task #10's — untouched.
_UPSERT_CHIP = """
    INSERT INTO chip_data_tw (ticker_id, trade_date, foreign_net, investment_trust_net,
                              dealer_net, margin_balance, block_trade_volume, source)
    VALUES ($1, $2, $3, $4, $5, $6, $7, 'twse_tpex')
    ON CONFLICT (ticker_id, trade_date) DO UPDATE SET
        foreign_net = EXCLUDED.foreign_net,
        investment_trust_net = EXCLUDED.investment_trust_net,
        dealer_net = EXCLUDED.dealer_net,
        margin_balance = EXCLUDED.margin_balance,
        block_trade_volume = EXCLUDED.block_trade_volume,
        source = EXCLUDED.source, ingested_at = now()
"""


def _chip_field(row: dict[str, Any], name: str, *, signed: bool) -> tuple[int | None, bool]:
    """Per-field validity (T9-M2 / A8 #4). Returns (value, ok):
    - missing/None -> (None, True): honest NULL, not an error (T9-M1);
    - NaN/inf/garbage, absurd magnitude, or a negative where the field can
      never be negative -> (None, False): reject the row, count it."""
    raw = row.get(name)
    if raw is None:
        return None, True
    value = _int(raw)
    if value is None:                       # NaN/inf/non-numeric
        return None, False
    if abs(value) > _MAX_CHIP_MAGNITUDE:    # poisoned magnitude
        return None, False
    if not signed and value < 0:            # e.g. negative margin balance
        return None, False
    return value, True


async def ingest_twse_tpex(
    conn: Any,
    client: TwseTpexClient,
    *,
    asof: date | None = None,
    sleeper=asyncio.sleep,
) -> ChipIngestStats:
    """Ingest chip facts for all covered TW tickers. Raises AdapterUnavailable
    only when NOTHING succeeded; partial failures are reported in stats."""
    stats = ChipIngestStats()

    # T9-S3 market routing: TWSE/TPEx rows only — US tickers never reach here.
    tickers = await conn.fetch(
        "SELECT id, symbol, exchange, full_symbol FROM ticker "
        "WHERE is_covered AND exchange IN ('TWSE','TPEx') ORDER BY id"
    )
    if not tickers:
        raise AdapterUnavailable("no covered TW tickers to ingest")

    for i, t in enumerate(tickers):
        if i > 0:
            await sleeper(PACING_DELAY_S)  # paced egress between tickers (A8 #3)
        try:
            await _ingest_one(conn, client, t, stats, sleeper=sleeper)
            stats.tickers_ok += 1
        except Exception as exc:  # per-ticker isolation (§22.4)
            stats.tickers_failed += 1
            stats.failures.append(f"{t['full_symbol']}: {exc}")
            # A8 #6 log hygiene: source+ticker+status only, never bodies.
            logger.warning("twse_tpex ingest failed for %s: %s", t["full_symbol"], exc)

    if stats.tickers_ok == 0:
        raise AdapterUnavailable(f"all tickers failed: {'; '.join(stats.failures)}")
    return stats


async def _ingest_one(
    conn: Any,
    client: TwseTpexClient,
    ticker: Any,
    stats: ChipIngestStats,
    sleeper=asyncio.sleep,
) -> None:
    symbol = ticker["symbol"]
    # A8 #1 / Y-1: fullmatch allowlist before any egress — the bare TW code
    # shapes the outbound TWSE/TPEx request.
    check_symbol(symbol)

    chip_rows = await _with_retries(client.fetch_daily_chip, symbol,
                                    ticker["exchange"], sleeper=sleeper)
    rows = []
    for raw in chip_rows:
        values, ok = [], True
        for name in _SIGNED_FIELDS:
            v, valid = _chip_field(raw, name, signed=True)
            values.append(v)
            ok = ok and valid
        for name in _UNSIGNED_FIELDS:
            v, valid = _chip_field(raw, name, signed=False)
            values.append(v)
            ok = ok and valid
        trade_date = raw.get("trade_date")
        # A row with no date, no valid field at all, or any poisoned field is
        # rejected cleanly — no half-row that 500s a later read (T9-M3).
        if not ok or not isinstance(trade_date, date) or all(v is None for v in values):
            stats.rows_skipped += 1
            continue
        rows.append((ticker["id"], trade_date, *values))
    if not rows:
        raise ValueError("no usable chip rows returned")
    await conn.executemany(_UPSERT_CHIP, rows)
    stats.rows_upserted += len(rows)
