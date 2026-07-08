"""Task #9 chip adapter tests — TEST_SLICE_task9_chip buckets 1/2/3/4 (+5's
3-part-key shape) and A8 #1/#3/#4 + XXE. No network anywhere."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.batch.adapters.common import PACING_DELAY_S, AdapterUnavailable
from app.batch.adapters.edgar_adapter import (
    Curated13F,
    Filer,
    FixtureEdgarClient,
    ingest_edgar_13f,
    load_curated_13f,
    parse_13f_info_table,
)
from app.batch.adapters.twse_tpex_adapter import (
    FixtureTwseTpexClient,
    ingest_twse_tpex,
)

ASOF = date(2026, 7, 8)
QTR = date(2026, 3, 31)
REPO_ROOT = Path(__file__).resolve().parent.parent

TW1 = {"id": 1, "symbol": "2330", "exchange": "TWSE", "full_symbol": "2330.TW"}
TW2 = {"id": 2, "symbol": "6488", "exchange": "TPEx", "full_symbol": "6488.TWO"}
US1 = {"id": 3, "symbol": "AAPL", "exchange": "US", "full_symbol": "AAPL"}
US2 = {"id": 4, "symbol": "TSLA", "exchange": "US", "full_symbol": "TSLA"}


async def no_sleep(_):  # retries/pacing without real delay
    return None


class FakeChipDb:
    """Captures chip upserts; enforces the TW market-routing filter (T9-S3)
    and the (ticker_id, trade_date) idempotency key + provenance (T9-B1)."""

    def __init__(self, tickers):
        self.tickers = tickers
        self.rows: list[tuple] = []

    async def fetch(self, query, *args):
        assert "FROM ticker" in query
        # routing is in the SQL itself: only TW exchanges may be selected
        assert "exchange IN ('TWSE','TPEx')" in query
        return [t for t in self.tickers if t["exchange"] in ("TWSE", "TPEx")]

    async def executemany(self, query, rows):
        assert "chip_data_tw" in query
        assert "ON CONFLICT (ticker_id, trade_date)" in query  # idempotent
        assert "ingested_at = now()" in query  # v1.2.5 provenance on update
        assert "'twse_tpex'" in query
        self.rows.extend(rows)


class ScriptedChipClient:
    """Deterministic scripted TW client: per-symbol rows/errors."""

    def __init__(self, rows=None, errors=None):
        self.rows = rows or {}
        self.errors = errors or {}
        self.calls: list[tuple] = []

    def fetch_daily_chip(self, symbol, exchange):
        self.calls.append((symbol, exchange))
        err = self.errors.get(symbol)
        if err:
            raise err
        return self.rows.get(symbol, [])


def chip_row(d=ASOF, **over):
    row = {"trade_date": d, "foreign_net": 1_500_000, "investment_trust_net": -20_000,
           "dealer_net": 5_000, "margin_balance": 12_000_000,
           "block_trade_volume": 300_000}
    row.update(over)
    return row


# --- TWSE/TPEx bucket 1: success + routing -----------------------------------

async def test_tw_success_routes_only_tw_tickers_and_upserts():
    db = FakeChipDb([TW1, TW2, US1])  # US ticker present but must never egress
    client = ScriptedChipClient(rows={"2330": [chip_row()], "6488": [chip_row()]})
    stats = await ingest_twse_tpex(db, client, asof=ASOF, sleeper=no_sleep)
    assert stats.tickers_ok == 2 and stats.tickers_failed == 0
    assert len(db.rows) == 2
    assert {c[0] for c in client.calls} == {"2330", "6488"}  # T9-S3: no AAPL
    # BIGINT binds are plain ints, trade_date a date
    tid, d, foreign, trust, dealer, margin, block = db.rows[0]
    assert (tid, d) == (1, ASOF)
    for v in (foreign, trust, dealer, margin, block):
        assert isinstance(v, int)


async def test_tw_negative_nets_stored_honestly():
    # T9-M2: three-institution net-SELL is a real market fact — never dropped.
    db = FakeChipDb([TW1])
    client = ScriptedChipClient(rows={"2330": [chip_row(foreign_net=-2_000_000,
                                                        dealer_net=-33_000)]})
    stats = await ingest_twse_tpex(db, client, asof=ASOF, sleeper=no_sleep)
    assert stats.rows_skipped == 0
    assert db.rows[0][2] == -2_000_000  # foreign_net
    assert db.rows[0][4] == -33_000     # dealer_net


async def test_tw_null_optional_fields_persist_as_null():
    # T9-M1: missing margin/block (real client limitation) -> NULL, no crash.
    db = FakeChipDb([TW1])
    client = ScriptedChipClient(rows={"2330": [chip_row(margin_balance=None,
                                                        block_trade_volume=None)]})
    stats = await ingest_twse_tpex(db, client, asof=ASOF, sleeper=no_sleep)
    assert stats.tickers_ok == 1 and stats.rows_skipped == 0
    assert db.rows[0][5] is None and db.rows[0][6] is None


# --- TWSE/TPEx bucket 2: missing / invalid ------------------------------------

async def test_tw_negative_margin_rejected_and_counted():
    # T9-M2 per-field spec: margin balance can NEVER be negative.
    db = FakeChipDb([TW1])
    client = ScriptedChipClient(rows={"2330": [chip_row(),
                                               chip_row(margin_balance=-1)]})
    stats = await ingest_twse_tpex(db, client, asof=ASOF, sleeper=no_sleep)
    assert len(db.rows) == 1
    assert stats.rows_skipped == 1  # counted, never silent


async def test_tw_nan_inf_and_absurd_rejected():
    db = FakeChipDb([TW1])
    bad = [chip_row(foreign_net=float("nan")),
           chip_row(dealer_net=float("inf")),
           chip_row(foreign_net=2e13)]  # absurd magnitude (A8 #4)
    client = ScriptedChipClient(rows={"2330": [chip_row(), *bad]})
    stats = await ingest_twse_tpex(db, client, asof=ASOF, sleeper=no_sleep)
    assert len(db.rows) == 1
    assert stats.rows_skipped == 3


async def test_tw_malformed_row_rejected_cleanly():
    # T9-M3: no trade_date / no values at all -> no half-row lands.
    db = FakeChipDb([TW1])
    client = ScriptedChipClient(rows={"2330": [
        chip_row(trade_date=None),
        {"trade_date": ASOF},  # every fact field missing
        chip_row(),
    ]})
    stats = await ingest_twse_tpex(db, client, asof=ASOF, sleeper=no_sleep)
    assert len(db.rows) == 1 and stats.rows_skipped == 2


# --- TWSE/TPEx bucket 3: outage / rate-limit / isolation ----------------------

async def test_tw_one_ticker_failure_never_aborts_peers():
    db = FakeChipDb([TW1, TW2])
    client = ScriptedChipClient(rows={"6488": [chip_row()]},
                                errors={"2330": RuntimeError("boom")})
    stats = await ingest_twse_tpex(db, client, asof=ASOF, sleeper=no_sleep)
    assert stats.tickers_ok == 1 and stats.tickers_failed == 1
    assert any("2330.TW" in f for f in stats.failures)
    assert len(db.rows) == 1  # 6488 still ingested


async def test_tw_total_outage_raises_adapter_unavailable():
    db = FakeChipDb([TW1, TW2])
    client = ScriptedChipClient(errors={"2330": RuntimeError("HTTP 503"),
                                        "6488": RuntimeError("HTTP 503")})
    with pytest.raises(AdapterUnavailable):
        await ingest_twse_tpex(db, client, asof=ASOF, sleeper=no_sleep)


async def test_tw_429_retries_with_backoff_then_succeeds():
    attempts = {"n": 0}
    delays: list[float] = []

    class FlakyChipClient(ScriptedChipClient):
        def fetch_daily_chip(self, symbol, exchange):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return [chip_row()]

    async def record_sleep(d):
        delays.append(d)

    db = FakeChipDb([TW1])
    stats = await ingest_twse_tpex(db, FlakyChipClient(), asof=ASOF,
                                   sleeper=record_sleep)
    assert stats.tickers_ok == 1
    assert attempts["n"] == 3  # bounded — no retry storm (T9-O4)
    # shared common._with_retries: exp backoff + jitter (A8 #3)
    assert len(delays) == 2
    assert 1.0 <= delays[0] <= 1.5
    assert 2.0 <= delays[1] <= 3.0


async def test_tw_requests_are_paced_between_tickers():
    delays: list[float] = []

    async def record_sleep(d):
        delays.append(d)

    db = FakeChipDb([TW1, TW2])
    client = ScriptedChipClient(rows={"2330": [chip_row()], "6488": [chip_row()]})
    await ingest_twse_tpex(db, client, asof=ASOF, sleeper=record_sleep)
    assert delays.count(PACING_DELAY_S) == 1  # N tickers -> N-1 pacing pauses


# --- TWSE/TPEx A8 #1: egress allowlist ----------------------------------------

async def test_tw_bad_symbol_rejected_before_egress():
    evil = {"id": 7, "symbol": "23;30", "exchange": "TWSE", "full_symbol": "23;30.TW"}
    sneaky = {"id": 8, "symbol": "2330\n", "exchange": "TWSE", "full_symbol": "2330.TW\n"}
    db = FakeChipDb([evil, sneaky, TW1])
    client = ScriptedChipClient(rows={"2330": [chip_row()]})
    stats = await ingest_twse_tpex(db, client, asof=ASOF, sleeper=no_sleep)
    assert stats.tickers_failed == 2
    assert any("allowlist" in f for f in stats.failures)
    # crucially: NO client call was made for the rejected symbols (Y-1 fullmatch)
    assert [c[0] for c in client.calls] == ["2330"]


# --- TWSE/TPEx bucket 4: deterministic fixture mode ---------------------------

async def test_tw_fixture_mode_is_deterministic():
    db1, db2 = FakeChipDb([TW1, TW2]), FakeChipDb([TW1, TW2])
    await ingest_twse_tpex(db1, FixtureTwseTpexClient(), asof=ASOF, sleeper=no_sleep)
    await ingest_twse_tpex(db2, FixtureTwseTpexClient(), asof=ASOF, sleeper=no_sleep)
    assert db1.rows == db2.rows  # byte-stable re-runs (FR-19 / T9-D1)
    assert len(db1.rows) == 2


def test_tw_fixture_produces_negative_nets_somewhere():
    # consecutive codes hit every seed residue: at least one net-sell day
    client = FixtureTwseTpexClient()
    nets = [client.fetch_daily_chip(s, "TWSE")[0]["foreign_net"]
            for s in ("1101", "1102", "1103")]
    assert any(n < 0 for n in nets)  # net-sell is generated, not sanitized away


# --- EDGAR 13F ---------------------------------------------------------------

class FakeEdgarDb:
    """Captures 13F upserts; enforces US routing (T9-S3) and the 3-part
    (ticker_id, quarter, filer_name) key + provenance (T9-B1)."""

    def __init__(self, tickers):
        self.tickers = tickers
        self.rows: list[tuple] = []

    async def fetch(self, query, *args):
        assert "FROM ticker" in query
        assert "exchange = 'US'" in query  # US-only routing in the SQL itself
        return [t for t in self.tickers if t["exchange"] == "US"]

    async def executemany(self, query, rows):
        assert "institutional_position_us" in query
        # T9-B1: the 3-part key — omitting filer_name would collapse filers.
        assert "ON CONFLICT (ticker_id, quarter, filer_name)" in query
        assert "ingested_at = now()" in query  # v1.2.5 provenance on update
        assert "'edgar_13f'" in query
        self.rows.extend(rows)


class ScriptedEdgarClient:
    """Deterministic scripted EDGAR client: per-CIK filings/errors."""

    def __init__(self, filings=None, errors=None):
        self.filings = filings or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    def fetch_latest_13f(self, cik):
        self.calls.append(cik)
        err = self.errors.get(cik)
        if err:
            raise err
        return self.filings[cik]


FILER_A = Filer(cik="0001067983", name="Berkshire Hathaway Inc")
FILER_B = Filer(cik="0001364742", name="BlackRock Inc.")
CURATED = Curated13F(filers=(FILER_A, FILER_B),
                     cusip_map={"AAPL": "037833100"})


def filing(name, holdings, quarter=QTR):
    return {"filer_name": name, "quarter": quarter, "holdings": holdings}


def aapl_holding(shares=1_000, value=250_000):
    return {"cusip": "037833100", "shares": shares, "value": value}


# --- EDGAR bucket 1: success / multi-filer ------------------------------------

async def test_edgar_multi_filer_yields_one_row_per_filer():
    db = FakeEdgarDb([TW1, US1])  # TW ticker present but must never route here
    client = ScriptedEdgarClient(filings={
        FILER_A.cik: filing(FILER_A.name, [aapl_holding(shares=1_000)]),
        FILER_B.cik: filing(FILER_B.name, [aapl_holding(shares=2_000)]),
    })
    stats = await ingest_edgar_13f(db, client, asof=ASOF, sleeper=no_sleep,
                                   curated=CURATED)
    assert stats.filers_ok == 2 and stats.rows_upserted == 2
    # T9-S6: N filers -> N rows under the same (ticker_id, quarter)
    assert {(r[0], r[1]) for r in db.rows} == {(3, QTR)}
    assert {r[2] for r in db.rows} == {FILER_A.cik, FILER_B.cik}
    # decimal-safe binds: BIGINT shares as int, NUMERIC value as Decimal
    assert isinstance(db.rows[0][3], int)
    assert isinstance(db.rows[0][4], Decimal)


async def test_edgar_unmapped_ticker_gets_zero_rows_not_error():
    # TSLA is covered but has no CUSIP mapping: honest zero rows (task #10
    # reads absence as chip-unavailable) — and unmapped CUSIPs are ignored.
    db = FakeEdgarDb([US1, US2])
    client = ScriptedEdgarClient(filings={
        FILER_A.cik: filing(FILER_A.name, [aapl_holding(),
                                           {"cusip": "88160R101",  # not mapped
                                            "shares": 10, "value": 1}]),
        FILER_B.cik: filing(FILER_B.name, []),
    })
    stats = await ingest_edgar_13f(db, client, asof=ASOF, sleeper=no_sleep,
                                   curated=CURATED)
    assert stats.filers_ok == 2 and stats.filers_failed == 0
    assert [r[0] for r in db.rows] == [3]  # only AAPL's ticker_id, no TSLA row


# --- EDGAR bucket 2: invalid holdings / hostile filer name ---------------------

async def test_edgar_negative_shares_rejected_and_counted():
    # 13F holdings can never be negative (unlike TW nets) — per-field spec.
    db = FakeEdgarDb([US1])
    client = ScriptedEdgarClient(filings={
        FILER_A.cik: filing(FILER_A.name, [aapl_holding(),
                                           aapl_holding(shares=-5),
                                           aapl_holding(value=-1),
                                           aapl_holding(shares=float("nan"))]),
        FILER_B.cik: filing(FILER_B.name, [aapl_holding()]),
    })
    stats = await ingest_edgar_13f(db, client, asof=ASOF, sleeper=no_sleep,
                                   curated=CURATED)
    assert stats.rows_skipped == 3
    assert stats.rows_upserted == 2


async def test_edgar_filer_key_is_cik_not_free_text():
    # Final ruling: the filer_name KEY column stores the 10-digit CIK — a
    # stable key immune to re-spelled/hostile institution names. Even a
    # filing reporting a hostile display name never contaminates the key.
    db = FakeEdgarDb([US1])
    client = ScriptedEdgarClient(filings={
        FILER_A.cik: filing("  Evil\r\nFiler\x00 Corp  ", [aapl_holding()]),
        FILER_B.cik: filing("B" * 300, [aapl_holding()]),
    })
    await ingest_edgar_13f(db, client, asof=ASOF, sleeper=no_sleep, curated=CURATED)
    keys = sorted(r[2] for r in db.rows)
    assert keys == sorted([FILER_A.cik, FILER_B.cik])  # 10-digit CIKs only
    import re as _re
    assert all(_re.fullmatch(r"\d{10}", k) for k in keys)


# --- EDGAR bucket 3: isolation / outage ----------------------------------------

async def test_edgar_per_filer_isolation():
    db = FakeEdgarDb([US1])
    client = ScriptedEdgarClient(
        filings={FILER_B.cik: filing(FILER_B.name, [aapl_holding()])},
        errors={FILER_A.cik: RuntimeError("boom")},
    )
    stats = await ingest_edgar_13f(db, client, asof=ASOF, sleeper=no_sleep,
                                   curated=CURATED)
    assert stats.filers_ok == 1 and stats.filers_failed == 1
    assert any(FILER_A.name in f for f in stats.failures)
    assert len(db.rows) == 1  # BlackRock still ingested


async def test_edgar_total_outage_raises_adapter_unavailable():
    db = FakeEdgarDb([US1])
    client = ScriptedEdgarClient(errors={
        FILER_A.cik: RuntimeError("HTTP 503"), FILER_B.cik: RuntimeError("HTTP 503"),
    })
    with pytest.raises(AdapterUnavailable):
        await ingest_edgar_13f(db, client, asof=ASOF, sleeper=no_sleep,
                               curated=CURATED)


# --- EDGAR A8: config validation at load (before any egress) -------------------

def test_curated_config_invalid_cik_rejected_at_load(tmp_path):
    # A8: a CIK shapes the outbound URL — malformed entries die at load,
    # so no client/egress can ever see them.
    bad = tmp_path / "curated.json"
    bad.write_text(json.dumps({"filers": [{"cik": "1067983", "name": "Short CIK"}],
                               "cusip_map": {}}))
    with pytest.raises(ValueError, match="invalid CIK"):
        load_curated_13f(bad)
    bad.write_text(json.dumps({"filers": [{"cik": "00010679AB", "name": "Alpha CIK"}],
                               "cusip_map": {}}))
    with pytest.raises(ValueError, match="invalid CIK"):
        load_curated_13f(bad)


def test_shipped_curated_config_loads_clean():
    curated = load_curated_13f(REPO_ROOT / "config" / "curated_13f.json")
    assert len(curated.filers) == 12
    assert all(len(f.cik) == 10 and f.cik.isdigit() for f in curated.filers)
    assert curated.cusip_map["AAPL"] == "037833100"


# --- EDGAR A8: XXE — defusedxml only -------------------------------------------

_XXE_XML = b"""<?xml version="1.0"?>
<!DOCTYPE informationTable [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<informationTable><infoTable><cusip>&xxe;</cusip></infoTable></informationTable>"""


def test_xxe_payload_rejected_with_zero_network(monkeypatch):
    import socket

    from defusedxml.common import DefusedXmlException

    def _no_net(*args, **kwargs):
        raise AssertionError("network egress during XML parse")

    monkeypatch.setattr(socket, "socket", _no_net)  # parse must stay offline
    with pytest.raises(DefusedXmlException):
        parse_13f_info_table(_XXE_XML)


def test_benign_namespaced_info_table_parses():
    xml = b"""<?xml version="1.0"?>
<ns1:informationTable
    xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <ns1:infoTable>
    <ns1:nameOfIssuer>APPLE INC</ns1:nameOfIssuer>
    <ns1:cusip>037833100</ns1:cusip>
    <ns1:value>250000</ns1:value>
    <ns1:shrsOrPrnAmt>
      <ns1:sshPrnamt>1000</ns1:sshPrnamt>
      <ns1:sshPrnamtType>SH</ns1:sshPrnamtType>
    </ns1:shrsOrPrnAmt>
  </ns1:infoTable>
</ns1:informationTable>"""
    assert parse_13f_info_table(xml) == [
        {"cusip": "037833100", "shares": "1000", "value": "250000"}
    ]


# --- EDGAR bucket 4: deterministic fixture mode ---------------------------------

async def test_edgar_fixture_mode_is_deterministic_and_multi_filer():
    db1, db2 = FakeEdgarDb([US1]), FakeEdgarDb([US1])
    await ingest_edgar_13f(db1, FixtureEdgarClient(CURATED), asof=ASOF,
                           sleeper=no_sleep, curated=CURATED)
    await ingest_edgar_13f(db2, FixtureEdgarClient(CURATED), asof=ASOF,
                           sleeper=no_sleep, curated=CURATED)
    assert db1.rows == db2.rows  # byte-stable re-runs (FR-19 / T9-D1)
    # curated filers all cover the mapped AAPL cusip -> multi-filer rows
    assert len(db1.rows) == 2
    assert {r[2] for r in db1.rows} == {FILER_A.cik, FILER_B.cik}


# --- A7 live pre-flight fixes: ROC dates + normalized TPEx keys ----------------

def test_roc_date_conversion():
    from app.batch.adapters.twse_tpex_adapter import _roc_to_gregorian

    assert _roc_to_gregorian("1150707") == date(2026, 7, 7)   # ROC 115
    assert _roc_to_gregorian("990101") == date(2010, 1, 1)    # ROC 99 (6-digit)
    assert _roc_to_gregorian("20260707") == date(2026, 7, 7)  # Gregorian passthrough
    assert _roc_to_gregorian("115/07/07") == date(2026, 7, 7) # separator-tolerant
    assert _roc_to_gregorian("garbage") is None
    assert _roc_to_gregorian("1151399") is None               # impossible month


def test_tpex_normalized_key_matching_against_live_schema():
    """Uses A7's ACTUAL live key dump: leading spaces, spaces before dashes,
    mid-word spaces, near-duplicate keys — exact literals must not be relied on."""
    from app.batch.adapters.twse_tpex_adapter import RealTwseTpexClient

    live_row = {
        "Date": "1150707",  # ROC
        "SecuritiesCompanyCode": "6488",
        "Dealers-Difference": "-1200",
        "Dealers -TotalSell": "9999",  # near-dupe with odd space — must not confuse
        "SecuritiesInvestmentTrustCompanies-Difference": "3400",
        "Foreign Investors include Mainland Area Investors "
        "(Foreign Dealers excluded)-Difference": "5600",
        "ForeignInvestorsInclude MainlandAreaInvestors-Difference": "9100",
        " Foreign Investors include Mainland Area Investors "
        "(Foreign Dealers excluded)-Total Sell": "1",
    }
    client = RealTwseTpexClient.__new__(RealTwseTpexClient)  # no network
    client._cache = {}
    client._response_date = {}
    import json as _json
    from unittest.mock import patch
    with patch("app.batch.adapters.twse_tpex_adapter.http_get",
               return_value=_json.dumps([live_row]).encode()):
        table = client._fetch_tpex()
    row = table["6488"]
    assert row["trade_date"] == date(2026, 7, 7)      # ROC converted, not ISO
    # dealer_net folds in the foreign-dealer component (A3 invariant):
    # -1200 + (9100 incl − 5600 excl) = 2300 — signed, right keys.
    assert row["dealer_net"] == 2300
    assert row["investment_trust_net"] == 3400
    assert row["foreign_net"] == 5600                  # dealers-EXCLUDED convention


def test_tpex_dealer_net_includes_foreign_dealer_component():
    """T9-S-MAP (A3 invariant): foreign dealers counted exactly once, in
    dealer_net — dealer += (foreign_incl − foreign_excl); foreign_net stays
    the dealers-EXCLUDED figure."""
    from unittest.mock import patch

    from app.batch.adapters.twse_tpex_adapter import RealTwseTpexClient

    live_row = {
        "Date": "1150707",
        "SecuritiesCompanyCode": "6488",
        "Dealers-Difference": "-1200",
        "SecuritiesInvestmentTrustCompanies-Difference": "3400",
        "Foreign Investors include Mainland Area Investors "
        "(Foreign Dealers excluded)-Difference": "5600",
        "ForeignInvestorsInclude MainlandAreaInvestors-Difference": "9100",
    }
    client = RealTwseTpexClient.__new__(RealTwseTpexClient)
    client._cache = {}
    client._response_date = {}
    import json as _json
    with patch("app.batch.adapters.twse_tpex_adapter.http_get",
               return_value=_json.dumps([live_row]).encode()):
        table = client._fetch_tpex()
    row = table["6488"]
    assert row["foreign_net"] == 5600            # dealers-excluded convention
    assert row["dealer_net"] == -1200 + (9100 - 5600)  # foreign dealers folded in
