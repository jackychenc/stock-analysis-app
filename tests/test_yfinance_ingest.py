"""Task #8 adapter tests — A6 buckets 1/2/3/4 + A8 #1/#3/#4, no network."""

from datetime import date
from decimal import Decimal

import pytest

from app.batch.adapters.yfinance_adapter import (
    AdapterUnavailable,
    FixtureYFinanceClient,
    ingest_yfinance,
)

ASOF = date(2026, 7, 8)


class FakeDb:
    """Captures upserts; emulates the two queries the adapter makes."""

    def __init__(self, tickers, existing_bars=0):
        self.tickers = tickers
        self.existing_bars = existing_bars
        self.bar_rows: list[tuple] = []
        self.fundamental_rows: list[tuple] = []
        self.sql_log: list[str] = []

    async def fetch(self, query, *args):
        assert "FROM ticker" in query
        return self.tickers

    async def fetchval(self, query, *args):
        assert "FROM price_bar" in query
        return self.existing_bars

    async def executemany(self, query, rows):
        self.sql_log.append(query)
        assert "ON CONFLICT (ticker_id, bar_date)" in query  # idempotent
        self.bar_rows.extend(rows)

    async def execute(self, query, *args):
        self.sql_log.append(query)
        assert "ON CONFLICT (ticker_id, asof_date)" in query  # idempotent
        self.fundamental_rows.append(args)


class ScriptedClient:
    """Deterministic scripted client: per-symbol bars/info/errors."""

    def __init__(self, bars=None, info=None, errors=None):
        self.bars = bars or {}
        self.info = info or {}
        self.errors = errors or {}
        self.calls: list[tuple] = []

    def fetch_daily_bars(self, symbol, period):
        self.calls.append(("bars", symbol, period))
        err = self.errors.get(symbol)
        if err:
            raise err
        return self.bars.get(symbol, [])

    def fetch_fundamentals(self, symbol):
        self.calls.append(("info", symbol))
        return self.info.get(symbol, {})


def good_bar(d=ASOF, close=100.0):
    return {"date": d, "open": close - 1, "high": close + 1,
            "low": close - 2, "close": close, "volume": 1000}


GOOD_INFO = {"trailingPE": 18.0, "priceToBook": 2.5, "enterpriseToEbitda": 10.0,
             "totalRevenue": 1e9, "trailingEps": 40.0, "grossMargins": 0.5,
             "operatingMargins": 0.3, "profitMargins": 0.2}

T1 = {"id": 1, "full_symbol": "2330.TW"}
T2 = {"id": 2, "full_symbol": "AAPL"}


async def no_sleep(_):  # retries without real delay
    return None


# --- bucket 1: success ------------------------------------------------------

async def test_success_persists_bars_and_fundamentals():
    db = FakeDb([T1, T2])
    client = ScriptedClient(
        bars={"2330.TW": [good_bar()], "AAPL": [good_bar(close=200.0)]},
        info={"2330.TW": GOOD_INFO, "AAPL": GOOD_INFO},
    )
    stats = await ingest_yfinance(db, client, asof=ASOF, sleeper=no_sleep)
    assert stats.tickers_ok == 2 and stats.tickers_failed == 0
    assert len(db.bar_rows) == 2 and len(db.fundamental_rows) == 2
    # decimal-safe: NUMERIC binds are Decimal, never float
    assert isinstance(db.bar_rows[0][2], Decimal)
    assert isinstance(db.fundamental_rows[0][2], Decimal)


async def test_backfill_vs_incremental_period():
    db = FakeDb([T1], existing_bars=0)
    client = ScriptedClient(bars={"2330.TW": [good_bar()]},
                            info={"2330.TW": GOOD_INFO})
    await ingest_yfinance(db, client, asof=ASOF, sleeper=no_sleep)
    assert ("bars", "2330.TW", "2y") in client.calls  # first run backfills

    db2 = FakeDb([T1], existing_bars=500)
    client2 = ScriptedClient(bars={"2330.TW": [good_bar()]},
                             info={"2330.TW": GOOD_INFO})
    await ingest_yfinance(db2, client2, asof=ASOF, sleeper=no_sleep)
    assert ("bars", "2330.TW", "7d") in client2.calls  # then incremental


# --- bucket 2: missing/invalid data ----------------------------------------

async def test_negative_eps_stored_honestly():
    info = dict(GOOD_INFO, trailingEps=-3.2)
    db = FakeDb([T1])
    client = ScriptedClient(bars={"2330.TW": [good_bar()]}, info={"2330.TW": info})
    await ingest_yfinance(db, client, asof=ASOF, sleeper=no_sleep)
    eps = db.fundamental_rows[0][6]  # eps position in the upsert binds
    assert eps == Decimal("-3.2")  # negative EPS is valid — never dropped


async def test_malformed_and_out_of_bounds_bars_skipped_counted():
    bars = [
        good_bar(),                                        # kept
        {"date": ASOF, "open": None, "high": 1, "low": 1,
         "close": 1, "volume": 1},                         # missing open
        {"date": ASOF, "open": float("nan"), "high": 1, "low": 1,
         "close": 1, "volume": 1},                         # NaN
        {"date": ASOF, "open": -5, "high": 1, "low": 1,
         "close": 1, "volume": 1},                         # negative price (A8 #4)
        {"date": ASOF, "open": 1, "high": 1, "low": 1,
         "close": 99999999999, "volume": 1},               # absurd magnitude
    ]
    db = FakeDb([T1])
    client = ScriptedClient(bars={"2330.TW": bars}, info={"2330.TW": GOOD_INFO})
    stats = await ingest_yfinance(db, client, asof=ASOF, sleeper=no_sleep)
    assert len(db.bar_rows) == 1
    assert stats.bars_skipped == 4  # counted, never silent


async def test_empty_fundamentals_flagged_but_bars_kept():
    db = FakeDb([T1])
    client = ScriptedClient(bars={"2330.TW": [good_bar()]},
                            info={"2330.TW": {}})
    stats = await ingest_yfinance(db, client, asof=ASOF, sleeper=no_sleep)
    assert stats.tickers_ok == 1
    assert len(db.bar_rows) == 1 and len(db.fundamental_rows) == 0
    assert any("fundamentals empty" in f for f in stats.failures)


# --- bucket 3: outage / rate-limit / isolation ------------------------------

async def test_one_ticker_failure_never_aborts_peers():
    db = FakeDb([T1, T2])
    client = ScriptedClient(
        bars={"AAPL": [good_bar(close=200.0)]},
        info={"AAPL": GOOD_INFO},
        errors={"2330.TW": RuntimeError("boom")},
    )
    stats = await ingest_yfinance(db, client, asof=ASOF, sleeper=no_sleep)
    assert stats.tickers_ok == 1 and stats.tickers_failed == 1
    assert any("2330.TW" in f for f in stats.failures)
    assert len(db.bar_rows) == 1  # AAPL still ingested


async def test_total_outage_raises_adapter_unavailable():
    db = FakeDb([T1, T2])
    client = ScriptedClient(errors={
        "2330.TW": RuntimeError("HTTP 503"), "AAPL": RuntimeError("HTTP 503"),
    })
    with pytest.raises(AdapterUnavailable):
        await ingest_yfinance(db, client, asof=ASOF, sleeper=no_sleep)


async def test_rate_limit_429_retries_with_backoff_then_succeeds():
    attempts = {"n": 0}
    delays: list[float] = []

    class FlakyClient(ScriptedClient):
        def fetch_daily_bars(self, symbol, period):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return [good_bar()]

    async def record_sleep(d):
        delays.append(d)

    db = FakeDb([T1])
    client = FlakyClient(info={"2330.TW": GOOD_INFO})
    stats = await ingest_yfinance(db, client, asof=ASOF, sleeper=record_sleep)
    assert stats.tickers_ok == 1
    assert attempts["n"] == 3  # bounded — no retry storm (T8-O5)
    # exponential backoff with jitter (A8 #3): base 1s then 2s, x1.0..1.5
    assert len(delays) == 2
    assert 1.0 <= delays[0] <= 1.5
    assert 2.0 <= delays[1] <= 3.0


async def test_requests_are_paced_between_tickers():
    from app.batch.adapters.yfinance_adapter import PACING_DELAY_S

    delays: list[float] = []

    async def record_sleep(d):
        delays.append(d)

    db = FakeDb([T1, T2])
    client = ScriptedClient(
        bars={"2330.TW": [good_bar()], "AAPL": [good_bar(close=200.0)]},
        info={"2330.TW": GOOD_INFO, "AAPL": GOOD_INFO},
    )
    await ingest_yfinance(db, client, asof=ASOF, sleeper=record_sleep)
    assert delays.count(PACING_DELAY_S) == 1  # N tickers -> N-1 pacing pauses


async def test_non_transient_error_does_not_retry():
    attempts = {"n": 0}

    class BadSymbolClient(ScriptedClient):
        def fetch_daily_bars(self, symbol, period):
            attempts["n"] += 1
            raise RuntimeError("HTTP 404 not found")

    db = FakeDb([T1])
    with pytest.raises(AdapterUnavailable):
        await ingest_yfinance(db, BadSymbolClient(), asof=ASOF, sleeper=no_sleep)
    assert attempts["n"] == 1  # no pointless retries on permanent errors


# --- A8 #1: egress allowlist -------------------------------------------------

async def test_bad_symbol_rejected_before_egress():
    evil = {"id": 9, "full_symbol": "EVIL/../?x=1"}
    db = FakeDb([evil, T2])
    client = ScriptedClient(bars={"AAPL": [good_bar(close=200.0)]},
                            info={"AAPL": GOOD_INFO})
    stats = await ingest_yfinance(db, client, asof=ASOF, sleeper=no_sleep)
    assert stats.tickers_failed == 1
    assert any("allowlist" in f for f in stats.failures)
    # crucially: NO client call was made for the rejected symbol
    assert all(call[1] != "EVIL/../?x=1" for call in client.calls)


# --- bucket 4: deterministic fixture mode ------------------------------------

async def test_fixture_mode_is_deterministic_and_offline():
    db1, db2 = FakeDb([T1, T2]), FakeDb([T1, T2])
    await ingest_yfinance(db1, FixtureYFinanceClient(), asof=ASOF, sleeper=no_sleep)
    await ingest_yfinance(db2, FixtureYFinanceClient(), asof=ASOF, sleeper=no_sleep)
    assert db1.bar_rows == db2.bar_rows  # byte-stable re-runs (FR-19)
    assert db1.fundamental_rows == db2.fundamental_rows
    assert len(db1.bar_rows) > 300  # backfill depth for backtest history
