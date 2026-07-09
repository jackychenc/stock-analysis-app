"""Task #14 — lens-detail read endpoints (contract v1.2.1 ModuleDetail
envelope): GET /stocks/{ticker}/technical|fundamentals|chip|news.

Honesty rules under test:
- rows present -> 200, status ok, per-lens series facts, score only if the
  batch persisted one;
- no rows -> status unavailable + empty series (never fabricated);
- news is the §4a ternary: fetch-ok + 0 headlines is ok/empty ("no news is
  neutral news"); no scheduled gdelt evidence (or a failed_tickers hit) is
  unavailable;
- uncovered/unknown ticker -> 404 SECTOR_NOT_COVERED on every lens route.
"""

from datetime import UTC, date, datetime

DETAIL = "/api/v1/stocks/{t}/{lens}"
LENSES = ("technical", "fundamentals", "chip", "news")


def _bar(ticker_id, d, close, volume=1000):
    return {"ticker_id": ticker_id, "bar_date": d, "open": close - 1.0,
            "high": close + 1.0, "low": close - 2.0, "close": close,
            "volume": volume}


# --- technical -------------------------------------------------------------------

def test_technical_ok_serves_bars_and_latest_indicators(web_client, store):
    store["price_bars"] = [
        _bar(1, date(2026, 7, 6), 100.0),
        _bar(1, date(2026, 7, 7), 102.0),
    ]
    store["technical_indicators"] = [{
        "ticker_id": 1, "calc_date": date(2026, 7, 7),
        "ma20": 99.5, "ma60": 95.0, "rsi14": 61.2,
        "macd": 0.8, "macd_signal": 0.5, "macd_hist": 0.3, "score": 1.5,
    }]
    r = web_client.get(DETAIL.format(t="2330.TW", lens="technical"))
    assert r.status_code == 200
    body = r.json()
    assert body["module"] == "technical"
    assert body["status"] == "ok"
    assert body["signal_score"] == 1.5  # persisted by the batch, never derived
    assert body["as_of"] == "2026-07-07"
    assert len(body["series"]) == 2
    assert body["series"][0]["date"] == "2026-07-06"  # ascending for charting
    assert body["series"][0]["close"] == 100.0
    assert body["series"][0]["volume"] == 1000
    # Latest indicators folded onto their matching bar.
    last = body["series"][-1]
    assert last["date"] == "2026-07-07"
    assert last["ma20"] == 99.5 and last["rsi14"] == 61.2
    assert last["macd_hist"] == 0.3


def test_technical_bars_without_indicators_is_ok_with_null_score(web_client, store):
    store["price_bars"] = [_bar(1, date(2026, 7, 7), 102.0)]
    body = web_client.get(DETAIL.format(t="2330.TW", lens="technical")).json()
    assert body["status"] == "ok"
    assert body["signal_score"] is None  # no indicator row -> no fabricated score
    assert body["as_of"] == "2026-07-07"  # latest bar date is still a data date
    assert len(body["series"]) == 1 and "ma20" not in body["series"][0]


def test_technical_no_bars_is_honest_unavailable(web_client):
    body = web_client.get(DETAIL.format(t="2330.TW", lens="technical")).json()
    assert body == {"module": "technical", "status": "unavailable",
                    "signal_score": None, "as_of": None, "series": []}


# --- fundamentals ----------------------------------------------------------------

def test_fundamentals_ok_serves_latest_snapshot(web_client, store):
    store["fundamentals"] = [
        {"ticker_id": 1, "asof_date": date(2026, 6, 30), "pe": 21.0, "pb": 5.5,
         "ev_ebitda": None, "revenue": 900e9, "eps": 40.0, "gross_margin": 0.55,
         "op_margin": 0.44, "net_margin": 0.40, "score": None},
        {"ticker_id": 1, "asof_date": date(2026, 7, 7), "pe": 22.5, "pb": 5.8,
         "ev_ebitda": 15.1, "revenue": 1e12, "eps": 42.0, "gross_margin": 0.56,
         "op_margin": 0.45, "net_margin": 0.41, "score": 1.0},
    ]
    body = web_client.get(DETAIL.format(t="2330.TW", lens="fundamentals")).json()
    assert body["module"] == "fundamental"
    assert body["status"] == "ok"
    assert body["signal_score"] == 1.0
    assert body["as_of"] == "2026-07-07"
    assert body["series"] == [{
        "as_of": "2026-07-07", "pe": 22.5, "pb": 5.8, "ev_ebitda": 15.1,
        "revenue": 1e12, "eps": 42.0, "gross_margin": 0.56,
        "op_margin": 0.45, "net_margin": 0.41,
    }]  # exactly the latest snapshot, actual columns, one dict


def test_fundamentals_no_snapshot_is_honest_unavailable(web_client):
    body = web_client.get(DETAIL.format(t="2330.TW", lens="fundamentals")).json()
    assert body["status"] == "unavailable"
    assert body["series"] == [] and body["signal_score"] is None


# --- chip ------------------------------------------------------------------------

def test_chip_tw_serves_daily_facts_and_latest_scored_row(web_client, store):
    store["chip_tw"] = [
        {"ticker_id": 1, "trade_date": date(2026, 7, 6), "foreign_net": -500,
         "investment_trust_net": 200, "dealer_net": 50, "margin_balance": 9000,
         "block_trade_volume": 10, "score": -0.4},
        {"ticker_id": 1, "trade_date": date(2026, 7, 7), "foreign_net": 1200,
         "investment_trust_net": 300, "dealer_net": -100, "margin_balance": None,
         "block_trade_volume": None, "score": 1.2},
    ]
    body = web_client.get(DETAIL.format(t="2330.TW", lens="chip")).json()
    assert body["module"] == "chip" and body["status"] == "ok"
    assert body["signal_score"] == 1.2  # newest scored row
    assert body["as_of"] == "2026-07-07"
    assert [d["trade_date"] for d in body["series"]] == ["2026-07-06", "2026-07-07"]
    latest = body["series"][-1]
    assert latest["foreign_net"] == 1200
    assert latest["investment_trust_net"] == 300
    assert latest["dealer_net"] == -100
    assert latest["margin_balance"] is None  # partial aux served as-is, not faked
    assert latest["score"] == 1.2


def test_chip_us_serves_13f_quarter_aggregates(web_client, store):
    store["us_positions"] = [
        {"ticker_id": 2, "quarter": date(2026, 3, 31), "filer_name": "A",
         "shares": 100, "score": None},
        {"ticker_id": 2, "quarter": date(2026, 3, 31), "filer_name": "B",
         "shares": 300, "score": None},
        {"ticker_id": 2, "quarter": date(2025, 12, 31), "filer_name": "A",
         "shares": 250, "score": None},
    ]
    body = web_client.get(DETAIL.format(t="AAPL", lens="chip")).json()
    assert body["status"] == "ok"
    assert body["as_of"] == "2026-03-31"
    assert body["series"] == [
        {"quarter": "2025-12-31", "total_shares": 250, "filer_count": 1},
        {"quarter": "2026-03-31", "total_shares": 400, "filer_count": 2},
    ]
    assert body["signal_score"] is None  # no persisted US score -> never invented


def test_chip_us_single_quarter_still_serves_the_facts(web_client, store):
    # Detail page shows FACTS; the flow-lens single-quarter honesty (task #21
    # unavailable) lives in the recommendation breakdown, not here.
    store["us_positions"] = [
        {"ticker_id": 2, "quarter": date(2026, 3, 31), "filer_name": "A",
         "shares": 100, "score": None},
    ]
    body = web_client.get(DETAIL.format(t="AAPL", lens="chip")).json()
    assert body["status"] == "ok"
    assert body["series"] == [
        {"quarter": "2026-03-31", "total_shares": 100, "filer_count": 1}
    ]


def test_chip_no_rows_is_honest_unavailable(web_client):
    for t in ("2330.TW", "AAPL"):  # both market branches
        body = web_client.get(DETAIL.format(t=t, lens="chip")).json()
        assert body["status"] == "unavailable"
        assert body["series"] == [] and body["signal_score"] is None


# --- news ------------------------------------------------------------------------

def _gdelt_run(run_date, status="ok", message="ok", run_kind="scheduled"):
    return {"run_date": run_date, "source_name": "gdelt", "status": status,
            "message": message, "run_kind": run_kind}


def test_news_ok_serves_windowed_headlines_desc(web_client, store):
    store["pipeline_runs"] = [_gdelt_run(date(2026, 7, 7))]
    store["news_items"] = [
        {"ticker_id": 1, "published_at": datetime(2026, 7, 5, 8, 0, tzinfo=UTC),
         "headline": "older", "url": "https://n/1", "source_name": "wire",
         "sentiment": -0.2},
        {"ticker_id": 1, "published_at": datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
         "headline": "newer", "url": "https://n/2", "source_name": "wire",
         "sentiment": 0.6},
        # Outside the 7d window: must not leak into the series.
        {"ticker_id": 1, "published_at": datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
         "headline": "ancient", "url": "https://n/0", "source_name": "wire",
         "sentiment": 0.9},
    ]
    body = web_client.get(DETAIL.format(t="2330.TW", lens="news")).json()
    assert body["module"] == "news" and body["status"] == "ok"
    assert body["as_of"] == "2026-07-07"  # the fetch evidence date
    assert [d["headline"] for d in body["series"]] == ["newer", "older"]  # desc
    assert body["series"][0]["sentiment"] == 0.6
    assert body["series"][0]["url"] == "https://n/2"


def test_news_fetch_ok_but_empty_window_is_ok_with_empty_series(web_client, store):
    # §4a: "no news is neutral news" — fetch-ok + 0 rows is NOT unavailable.
    store["pipeline_runs"] = [_gdelt_run(date(2026, 7, 7))]
    body = web_client.get(DETAIL.format(t="2330.TW", lens="news")).json()
    assert body["status"] == "ok"
    assert body["series"] == []


def test_news_without_scheduled_run_is_unavailable(web_client, store):
    # No gdelt evidence at all — row count can never stand in for the fetch
    # outcome (A8 integrity rule), so this is unavailable even if items exist.
    store["news_items"] = [
        {"ticker_id": 1, "published_at": datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
         "headline": "orphan", "url": None, "source_name": None, "sentiment": 0.1},
    ]
    body = web_client.get(DETAIL.format(t="2330.TW", lens="news")).json()
    assert body["status"] == "unavailable"
    assert body["series"] == []


def test_news_failed_run_or_failed_ticker_is_unavailable(web_client, store):
    store["pipeline_runs"] = [_gdelt_run(date(2026, 7, 7), status="error")]
    body = web_client.get(DETAIL.format(t="2330.TW", lens="news")).json()
    assert body["status"] == "unavailable"

    store["pipeline_runs"] = [
        _gdelt_run(date(2026, 7, 7), message="failed_tickers=2330.TW,AAPL")]
    body = web_client.get(DETAIL.format(t="2330.TW", lens="news")).json()
    assert body["status"] == "unavailable"  # this ticker's fetch failed


# --- coverage gate ---------------------------------------------------------------

def test_every_lens_detail_route_gates_uncovered_and_unknown_tickers(web_client):
    for lens in LENSES:
        for t in ("XXXX.TW", "NOPE"):
            r = web_client.get(DETAIL.format(t=t, lens=lens))
            assert r.status_code == 404, f"{lens}/{t}"
            assert r.json()["detail"]["code"] == "SECTOR_NOT_COVERED"


def test_lens_detail_requires_auth(client):
    for lens in LENSES:
        assert client.get(DETAIL.format(t="2330.TW", lens=lens)).status_code == 401
