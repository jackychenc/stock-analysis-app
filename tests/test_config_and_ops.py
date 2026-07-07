"""Config weights validation + pipeline status + health + lens stubs."""

from app.schemas.contracts import SCORING_MODULES


def test_get_weights_defaults(web_client):
    r = web_client.get("/api/v1/config/weights")
    assert r.status_code == 200
    body = r.json()
    assert body["module_weights"] == {
        "technical": 0.30, "fundamental": 0.30, "chip": 0.25, "news": 0.15
    }
    assert body["horizon_months"] == 6


def test_put_weights_must_sum_to_one(web_client):
    r = web_client.put("/api/v1/config/weights", json={
        "module_weights": {"technical": 0.5, "fundamental": 0.3, "chip": 0.2, "news": 0.15},
        "horizon_months": 6,
    })
    assert r.status_code == 422


def test_put_weights_valid_update(web_client):
    r = web_client.put("/api/v1/config/weights", json={
        "module_weights": {"technical": 0.4, "fundamental": 0.3, "chip": 0.2, "news": 0.1},
        "horizon_months": 12,
    })
    assert r.status_code == 200
    assert r.json()["horizon_months"] == 12


def test_pipeline_status_lists_all_sources(web_client):
    r = web_client.get("/api/v1/pipeline/status")
    assert r.status_code == 200
    body = r.json()
    names = {s["source_name"] for s in body["sources"]}
    assert names == {"yfinance", "twse_tpex", "edgar_13f", "gdelt"}
    assert all(s["status"] == "never_run" for s in body["sources"])


def test_healthz_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_lens_detail_stubs_are_contract_shaped(web_client):
    for path, module in (
        ("technical", "technical"), ("fundamentals", "fundamental"),
        ("news", "news"), ("chip", "chip"),
    ):
        r = web_client.get(f"/api/v1/stocks/2330.TW/{path}")
        assert r.status_code == 200
        assert r.json()["module"] == module
        assert r.json()["status"] == "unavailable"


def test_backtest_reports_insufficient_history(web_client):
    r = web_client.get("/api/v1/stocks/2330.TW/backtest")
    assert r.status_code == 200
    body = r.json()
    assert body["insufficient_history"] is True
    assert body["rolling_accuracy_full"] is None  # no misleading number
    assert body["benchmark"] == "^TWII"


def test_backtest_us_benchmark(web_client):
    r = web_client.get("/api/v1/stocks/AAPL/backtest")
    assert r.json()["benchmark"] == "^GSPC"


def test_supply_chain_stub(web_client):
    r = web_client.get("/api/v1/stocks/2330.TW/supply-chain")
    assert r.status_code == 200
    assert r.json() == {"nodes": [], "edges": []}


def test_all_scoring_modules_constant():
    assert SCORING_MODULES == ("technical", "fundamental", "chip", "news")
