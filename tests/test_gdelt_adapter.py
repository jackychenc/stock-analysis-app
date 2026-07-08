"""Task #12 GDELT+VADER adapter tests — the contract v1.2.8 §4a QA/security
gates: sanitize-on-ingest, URL scheme gate, VADER range/input integrity,
allowlist-before-egress + no-article-URL egress (SSRF boundary), query
building, 429 retry bounds, empty-vs-error fetch divergence, idempotency,
seendate parsing. No network anywhere."""

import json
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.batch.adapters.common import PACING_DELAY_S, AdapterUnavailable, http_get
from app.batch.adapters.gdelt_adapter import (
    ALLOWED_HOSTS,
    FixtureGdeltClient,
    RealGdeltClient,
    build_query,
    ingest_gdelt,
    load_news_queries,
    parse_seendate,
    sanitize_text,
    validate_url,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

TW1 = {"id": 1, "full_symbol": "2330.TW"}
TW2 = {"id": 2, "full_symbol": "6488.TWO"}
US1 = {"id": 3, "full_symbol": "AAPL"}

QUERIES = {
    "2330.TW": ("TSMC", "Taiwan Semiconductor"),
    "6488.TWO": ("GlobalWafers",),
    "AAPL": ("Apple Inc", "AAPL"),
}


async def no_sleep(_):  # retries/pacing without real delay
    return None


class FakeNewsDb:
    """Captures news inserts; emulates the UNIQUE(ticker_id, url, published_at)
    key with DO NOTHING semantics (idempotency) and checks the insert shape."""

    def __init__(self, tickers):
        self.tickers = tickers
        self.rows: dict[tuple, tuple] = {}

    async def fetch(self, query, *args):
        assert "FROM ticker" in query and "is_covered" in query
        return self.tickers

    async def executemany(self, query, rows):
        assert "news_item" in query
        assert "ON CONFLICT (ticker_id, url, published_at) DO NOTHING" in query
        for r in rows:
            # (ticker_id, published_at, headline, url, source_name, sentiment, score)
            self.rows.setdefault((r[0], r[3], r[1]), r)  # first write wins


class ScriptedGdeltClient:
    """Deterministic scripted client: per-symbol articles/errors."""

    def __init__(self, articles=None, errors=None):
        self.articles = articles or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, str]] = []

    def fetch_articles(self, full_symbol, query):
        self.calls.append((full_symbol, query))
        err = self.errors.get(full_symbol)
        if err:
            raise err
        return self.articles.get(full_symbol, [])


class StubAnalyzer:
    """VADER stand-in: fixed compound, records exactly what text it was fed."""

    def __init__(self, compound=0.5):
        self.compound = compound
        self.inputs: list[str] = []

    def polarity_scores(self, text):
        self.inputs.append(text)
        return {"compound": self.compound, "neg": 0.0, "neu": 1.0, "pos": 0.0}


def art(**over):
    a = {"title": "TSMC posts record profit", "url": "https://news.example.com/a1",
         "seendate": "20260707T120000Z", "domain": "news.example.com"}
    a.update(over)
    return a


async def run_one(articles, analyzer=None, tickers=None):
    db = FakeNewsDb(tickers or [TW1])
    client = ScriptedGdeltClient(articles={"2330.TW": articles})
    stats = await ingest_gdelt(db, client, queries=QUERIES, sleeper=no_sleep,
                               analyzer=analyzer or StubAnalyzer())
    return db, stats


# --- gate 1/2: sanitize on ingest ----------------------------------------------

def test_sanitize_controls_whitespace_nfc_cap_and_literal_html():
    assert sanitize_text("a\x00b\x07c") == "abc"                # Cc + NUL stripped
    assert sanitize_text("zero\u200bwidth\u00ad") == "zerowidth"  # Cf stripped
    assert sanitize_text("  foo \t\n\r  bar  ") == "foo bar"     # runs collapsed
    assert sanitize_text("Cafe\u0301") == "Caf\u00e9"             # NFC applied
    assert len(sanitize_text("x" * 600)) == 500                  # capped
    # HTML tags KEPT as literal text — React escapes at render, we never strip
    assert sanitize_text("<b>TSMC</b> up") == "<b>TSMC</b> up"
    assert sanitize_text("\x00 \u200b \x1f") == ""              # empty after


async def test_empty_after_sanitize_drops_item():
    db, stats = await run_one([art(title="\x00\u200b \x07")])
    assert stats.items_rejected == 1 and len(db.rows) == 0
    assert stats.tickers_empty == 1  # fetch was OK — not a failure


async def test_malicious_headline_stored_without_control_chars():
    db, stats = await run_one([art(title="<script>alert(1)</script>\x00\x07evil")])
    assert len(db.rows) == 1
    headline = next(iter(db.rows.values()))[2]
    assert headline == "<script>alert(1)</script>evil"  # script tag literal text
    assert not any(ord(c) < 0x20 for c in headline)     # no control chars stored


# --- gate 3: URL scheme gate -----------------------------------------------------

async def test_non_http_url_schemes_drop_the_whole_item():
    db, stats = await run_one([
        art(url="javascript:alert(1)"),
        art(url="data:text/html;base64,PHNjcmlwdD4="),
        art(url="ftp://evil.example.com/x"),
        art(url="https://kept.example.com/story"),
        art(url="https://long.example.com/" + "a" * 2048),  # > 2048 cap
    ])
    assert stats.items_rejected == 4
    assert [r[3] for r in db.rows.values()] == ["https://kept.example.com/story"]


def test_validate_url_gate_directly():
    assert validate_url("https://ok.example.com/x") == "https://ok.example.com/x"
    assert validate_url("http://ok.example.com/x") == "http://ok.example.com/x"
    assert validate_url("javascript:alert(1)") is None
    assert validate_url(None) is None
    assert validate_url("https://ok.example.com/\x00") is None


# --- gates 4/5: VADER range + input integrity -------------------------------------

async def test_vader_compound_out_of_range_rejects_row_not_clamped():
    db, stats = await run_one([art()], analyzer=StubAnalyzer(compound=1.5))
    assert len(db.rows) == 0
    assert stats.items_rejected == 1  # rejected, never clamped into [-1,1]


async def test_vader_receives_the_sanitized_capped_text():
    analyzer = StubAnalyzer()
    oversized = "great " * 120 + "\x00\x07tail"  # >500 chars + control chars
    db, _ = await run_one([art(title=oversized)], analyzer=analyzer)
    assert analyzer.inputs == [sanitize_text(oversized)]
    assert len(analyzer.inputs[0]) == 500  # VADER fed the capped text only
    assert next(iter(db.rows.values()))[2] == analyzer.inputs[0]  # DB == VADER input


async def test_sentiment_stored_4dp_and_score_2x_clamped_2dp():
    db, _ = await run_one([art()], analyzer=StubAnalyzer(compound=0.33335))
    row = next(iter(db.rows.values()))
    sentiment, score = row[5], row[6]
    assert sentiment == Decimal("0.3334") and isinstance(sentiment, Decimal)
    assert score == Decimal("0.67") and isinstance(score, Decimal)


# --- gates 6/7: SSRF boundary — allowlist before egress, no article-URL fetch -----

def test_allowlist_blocks_non_gdelt_host_before_any_network(monkeypatch):
    import socket

    def _no_net(*args, **kwargs):
        raise AssertionError("network egress before allowlist check")

    monkeypatch.setattr(socket, "socket", _no_net)  # any socket == test failure
    with pytest.raises(ValueError, match="allowlist"):
        http_get("https://evil.example.com/api/v2/doc/doc",
                 allowed_hosts=ALLOWED_HOSTS, max_bytes=1024)
    with pytest.raises(ValueError, match="https"):
        http_get("http://api.gdeltproject.org/api/v2/doc/doc",  # downgrade
                 allowed_hosts=ALLOWED_HOSTS, max_bytes=1024)


async def test_article_urls_are_stored_as_data_never_fetched(monkeypatch):
    """SSRF boundary: fixture articles carry third-party URLs; the transport
    must only ever see api.gdeltproject.org requests — one per ticker."""
    seen_urls: list[str] = []

    def fake_http_get(url, **kwargs):
        seen_urls.append(url)
        return json.dumps({"articles": [
            art(url="https://attacker-controlled.example.com/lure"),
        ]}).encode()

    monkeypatch.setattr("app.batch.adapters.gdelt_adapter.http_get", fake_http_get)
    db = FakeNewsDb([TW1])
    stats = await ingest_gdelt(db, RealGdeltClient(), queries=QUERIES,
                               sleeper=no_sleep, analyzer=StubAnalyzer())
    assert stats.headlines_ingested == 1
    assert len(seen_urls) == 1  # the GDELT query only — zero article fetches
    assert all(urllib.parse.urlparse(u).hostname == "api.gdeltproject.org"
               for u in seen_urls)
    # the third-party URL landed in the DB as data
    assert next(iter(db.rows.values()))[3] == (
        "https://attacker-controlled.example.com/lure")


# --- gate 8: query building --------------------------------------------------------

def test_build_query_curated_phrases_or_joined_with_sourcelang():
    assert build_query("2330.TW", QUERIES) == (
        '("TSMC" OR "Taiwan Semiconductor") sourcelang:eng')
    assert build_query("6488.TWO", QUERIES) == '"GlobalWafers" sourcelang:eng'


def test_build_query_unlisted_ticker_falls_back_to_quoted_symbol():
    assert build_query("2317.TW", QUERIES) == '"2317.TW" sourcelang:eng'


def test_build_query_invalid_phrase_named_rejection():
    with pytest.raises(ValueError, match="egress allowlist"):
        build_query("2330.TW", {"2330.TW": ("TSMC; DROP TABLE",)})


async def test_invalid_phrase_never_egresses_via_ingest():
    db = FakeNewsDb([TW1])
    client = ScriptedGdeltClient(articles={"2330.TW": [art()]})
    with pytest.raises(AdapterUnavailable):  # sole ticker failed pre-egress
        await ingest_gdelt(db, client, queries={"2330.TW": ("bad;phrase",)},
                           sleeper=no_sleep, analyzer=StubAnalyzer())
    assert client.calls == []  # crucially: NO fetch was ever attempted


def test_curated_config_invalid_phrase_rejected_at_load(tmp_path):
    bad = tmp_path / "queries.json"
    bad.write_text(json.dumps({"2330.TW": ["TSMC", "evil'); --phrase\x00"]}))
    with pytest.raises(ValueError, match="invalid query phrase"):
        load_news_queries(bad)


def test_shipped_news_queries_config_loads_clean():
    queries = load_news_queries(REPO_ROOT / "config" / "news_queries.json")
    assert queries["2330.TW"] == ("TSMC", "Taiwan Semiconductor")
    assert queries["6488.TWO"] == ("GlobalWafers",)
    assert "_comment" not in queries  # comment key skipped, never a query


# --- gate 9: 429 transient handling -------------------------------------------------

async def test_429_retried_with_backoff_then_succeeds():
    attempts = {"n": 0}
    delays: list[float] = []

    class Flaky(ScriptedGdeltClient):
        def fetch_articles(self, full_symbol, query):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return [art()]

    async def record_sleep(d):
        delays.append(d)

    db = FakeNewsDb([TW1])
    stats = await ingest_gdelt(db, Flaky(), queries=QUERIES,
                               sleeper=record_sleep, analyzer=StubAnalyzer())
    assert stats.tickers_ok == 1 and attempts["n"] == 3  # bounded, no storm
    # shared common._with_retries: exp backoff + jitter (A8 #3)
    assert len(delays) == 2
    assert 1.0 <= delays[0] <= 1.5
    assert 2.0 <= delays[1] <= 3.0


async def test_persistent_429_fails_ticker_after_bounded_retries():
    db = FakeNewsDb([TW1, US1])
    client = ScriptedGdeltClient(
        articles={"AAPL": [art(url="https://news.example.com/aapl")]},
        errors={"2330.TW": RuntimeError("HTTP 429 Too Many Requests")},
    )
    stats = await ingest_gdelt(db, client, queries=QUERIES, sleeper=no_sleep,
                               analyzer=StubAnalyzer())
    assert stats.failed_symbols == ["2330.TW"]  # exhausted retries -> failed
    assert "failed_tickers=2330.TW" in stats.summary()  # machine-stable token
    assert stats.tickers_ok == 1  # per-ticker isolation: AAPL still ingested


# --- gate 10: empty-vs-error fetch divergence (the §4a discriminator) ---------------

async def test_empty_body_and_no_articles_key_are_fetch_ok_zero_results(monkeypatch):
    bodies = {"TSMC": b"", "GlobalWafers": json.dumps({"status": "ok"}).encode(),
              "Apple": b"<html><body>503 upstream error page</body></html>"}

    def fake_http_get(url, **kwargs):
        for marker, body in bodies.items():
            if urllib.parse.quote(marker) in url:
                return body
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("app.batch.adapters.gdelt_adapter.http_get", fake_http_get)
    db = FakeNewsDb([TW1, TW2, US1])
    stats = await ingest_gdelt(db, RealGdeltClient(), queries=QUERIES,
                               sleeper=no_sleep, analyzer=StubAnalyzer())
    # empty body + JSON-without-articles: fetch OK, honest 0 results...
    assert stats.tickers_empty == 2
    # ...but a non-JSON non-empty body (HTML error page) is a FAILED query —
    # the two paths DIVERGE and the failure is named in the token.
    assert stats.failed_symbols == ["AAPL"]
    assert "failed_tickers=AAPL" in stats.summary()


async def test_all_tickers_failed_raises_adapter_unavailable():
    db = FakeNewsDb([TW1, US1])
    client = ScriptedGdeltClient(errors={
        "2330.TW": RuntimeError("HTTP 503"), "AAPL": RuntimeError("HTTP 503"),
    })
    with pytest.raises(AdapterUnavailable):  # source effectively down
        await ingest_gdelt(db, client, queries=QUERIES, sleeper=no_sleep,
                           analyzer=StubAnalyzer())


# --- gate 11 + fixture mode: idempotency, determinism, empty-neutral path -----------

async def test_fixture_ingest_twice_is_idempotent():
    db = FakeNewsDb([TW1, TW2, US1])
    await ingest_gdelt(db, FixtureGdeltClient(), queries=QUERIES, sleeper=no_sleep)
    first = dict(db.rows)
    await ingest_gdelt(db, FixtureGdeltClient(), queries=QUERIES, sleeper=no_sleep)
    assert db.rows == first  # UNIQUE key + DO NOTHING: row count unchanged
    assert len(db.rows) == 3  # 2330.TW x2 + AAPL x1 (real VADER, no network)


async def test_fixture_covers_the_empty_neutral_path_and_counts():
    db = FakeNewsDb([TW1, TW2, US1])
    stats = await ingest_gdelt(db, FixtureGdeltClient(), queries=QUERIES,
                               sleeper=no_sleep)
    assert stats.tickers_ok == 2
    assert stats.tickers_empty == 1  # 6488.TWO: fetch ok, 0 articles (FR-19)
    assert stats.failed_symbols == [] and stats.items_rejected == 0
    assert "failed_tickers=" not in stats.summary()  # token only on failure
    for row in db.rows.values():
        assert Decimal("-1") <= row[5] <= Decimal("1")  # real VADER compounds


async def test_pacing_between_tickers():
    delays: list[float] = []

    async def record_sleep(d):
        delays.append(d)

    db = FakeNewsDb([TW1, TW2, US1])
    await ingest_gdelt(db, FixtureGdeltClient(), queries=QUERIES,
                       sleeper=record_sleep)
    assert delays.count(PACING_DELAY_S) == 2  # N tickers -> N-1 pacing pauses


# --- gate 13: seendate parsing -------------------------------------------------------

def test_seendate_valid_and_garbage():
    assert parse_seendate("20260708T120000Z") == datetime(2026, 7, 8, 12, 0, 0,
                                                          tzinfo=UTC)
    assert parse_seendate("garbage") is None
    assert parse_seendate("20261399T120000Z") is None  # impossible month/day
    assert parse_seendate(None) is None


async def test_unparseable_seendate_drops_item():
    db, stats = await run_one([art(seendate="not-a-date"), art()])
    assert stats.items_rejected == 1
    assert len(db.rows) == 1


# --- co-gate assertions (A6 outcome lane + A8 mechanism lane) --------------------
# End-to-end chain: adapter fetch outcome -> pipeline_run row -> news_signal.
# RTM trace (A8 ACs, NFR-21 -> FR-15):
#   SEC-NEWS-SSRF        -> test_article_urls_are_stored_as_data_never_fetched
#   SEC-NEWS-SANITIZE    -> test_malicious_headline_stored_without_control_chars,
#                           test_vader_receives_the_sanitized_capped_text
#   SEC-NEWS-URLSCHEME   -> test_non_http_url_schemes_drop_the_whole_item
#   SEC-NEWS-VADER-BOUNDS-> test_vader_compound_out_of_range_rejects_row_not_clamped
#   SEC-NEWS-EGRESS      -> test_allowlist_blocks_non_gdelt_host_before_any_network,
#                           test_invalid_phrase_never_egresses_via_ingest
#   T12-M1-DIVERGENCE / T12-M1-FAILCLOSED -> the two tests below.


class _EndToEndConn(FakeNewsDb):
    """FakeNewsDb + the pipeline_run/news_item reads news_signal makes, so one
    fake serves the whole ingest->message->signal chain."""

    def __init__(self, tickers):
        super().__init__(tickers)
        self.run_row = None

    async def fetchrow(self, query, *args):
        assert "FROM pipeline_run" in query
        return self.run_row

    async def fetch(self, query, *args):
        if "FROM ticker" in query:
            return self.tickers
        assert "FROM news_item" in query
        ticker_id = args[0]
        return [{"sentiment": r[5]} for r in self.rows.values() if r[0] == ticker_id]


async def test_t12_m1_divergence_error_vs_empty_never_collapse():
    """T12-M1-DIVERGENCE (BLOCK if fails): a ticker-query error and a genuine
    0-headline result both leave 0 news_item rows — the breakdown status is
    the sole discriminator and the two paths must DIVERGE, never collapse."""
    from datetime import date

    from app.batch.signals.news import news_signal

    conn = _EndToEndConn([TW1, TW2])
    client = ScriptedGdeltClient(
        articles={"6488.TWO": []},  # clean fetch, legitimately empty
        errors={"2330.TW": ValueError("GDELT response is not parseable JSON")},
    )
    stats = await ingest_gdelt(conn, client, queries=QUERIES, sleeper=no_sleep,
                               analyzer=StubAnalyzer())
    assert stats.failed_symbols == ["2330.TW"] and stats.tickers_empty == 1
    conn.run_row = {"status": "ok", "message": f"[live] partial: {stats.summary()}"}

    asof = date(2026, 7, 8)
    errored = await news_signal(conn, 1, asof, "2330.TW")
    empty = await news_signal(conn, 2, asof, "6488.TWO")
    # error leg: unavailable -> engine renormalises (dc 0.75)
    assert errored.status == "unavailable" and errored.signal is None
    # empty leg: ok / neutral 0.00 / count-0 note -> full completeness (dc 1.0)
    assert empty.status == "ok" and empty.signal == Decimal("0.00")
    assert empty.note == "0 headlines in window"
    assert errored.status != empty.status  # diverge, never collapse


async def test_t12_m1_failclosed_fault_yielding_zero_items_never_neutral():
    """T12-M1-FAILCLOSED (BLOCK if fails, A8 mechanism lane): a fault injected
    mid-fetch (persistent 429 / TLS error / poisoned response) that still ends
    with 0 usable items must fail closed to `unavailable` — never degrade to a
    silent neutral 0.0 (which would mask adverse news)."""
    from datetime import date

    from app.batch.signals.news import news_signal

    for fault in (TimeoutError("TLS handshake timed out"),
                  RuntimeError("HTTP 429 too many requests"),
                  ValueError("GDELT response is not parseable JSON")):
        conn = _EndToEndConn([TW1, US1])
        client = ScriptedGdeltClient(articles={"AAPL": [art()]},
                                     errors={"2330.TW": fault})
        stats = await ingest_gdelt(conn, client, queries=QUERIES,
                                   sleeper=no_sleep, analyzer=StubAnalyzer())
        assert "2330.TW" in stats.failed_symbols
        conn.run_row = {"status": "ok",
                        "message": f"[live] partial: {stats.summary()}"}
        faulted = await news_signal(conn, 1, date(2026, 7, 8), "2330.TW")
        assert faulted.status == "unavailable", f"fault {fault!r} not fail-closed"
        assert faulted.signal is None  # never a fabricated neutral


# --- D-1 fail-closed totality (A8 in-gate, both sides required) -------------------
# Extends T12-M1-FAILCLOSED to the internal-data trigger: fail-closed must not
# depend on ticker-table hygiene.

async def test_d1_read_side_malformed_symbol_fail_closes_never_neutral():
    """D-1 read side: a non-SYMBOL_RE full_symbol can never trust the token
    (its own failure could have been unrepresentable) -> unavailable
    unconditionally — even when the source row reads ok and the window would
    read 0 rows (which would otherwise be neutral 0.0)."""
    from datetime import date

    from app.batch.signals.news import news_signal

    conn = _EndToEndConn([{"id": 9, "full_symbol": "BAD SYM"}])
    conn.run_row = {"status": "ok",
                    "message": "[live] tickers ok=1 empty=0 failed=0; "
                               "headlines ingested=2 rejected=0"}
    sig = await news_signal(conn, 9, date(2026, 7, 8), "BAD SYM")
    assert sig.status == "unavailable" and sig.signal is None
    assert sig.note == "symbol not token-safe"


async def test_d1_write_side_malformed_symbol_never_truncates_neighbours():
    """D-1 write side: a malformed symbol in failed_symbols must never
    truncate conforming neighbours out of the token — the neighbour's genuine
    failure must still read unavailable, never neutral. The failed= count
    stays the FULL count (evidence never silently lost)."""
    from datetime import date

    from app.batch.adapters.gdelt_adapter import NewsIngestStats
    from app.batch.signals.news import news_signal, parse_failed_tickers

    stats = NewsIngestStats(failed_symbols=["BAD SYM", "AAPL"])
    msg = stats.summary()
    assert "failed=2" in msg  # full count, not the token count
    # token contains ONLY the conforming neighbour — nothing to truncate on
    assert msg.endswith("failed_tickers=AAPL")
    assert parse_failed_tickers(msg) == frozenset({"AAPL"})

    conn = _EndToEndConn([US1])
    conn.run_row = {"status": "ok", "message": f"[live] partial: {msg}"}
    neighbour = await news_signal(conn, 3, date(2026, 7, 8), "AAPL")
    assert neighbour.status == "unavailable"  # genuinely failed -> never neutral
    assert neighbour.note == "gdelt ticker query failed"
