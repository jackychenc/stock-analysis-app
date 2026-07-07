"""Dashboard contract (openapi.yaml v1.0):
- ALWAYS all four scoring-module keys, each ok|unavailable.
- Non-empty per-lens breakdown on every recommendation.
- Single failed module => 200 with flag; out-of-scope => 404 SECTOR_NOT_COVERED.
"""

from app.schemas.contracts import SCORING_MODULES
from tests.conftest import make_recommendation

DASH = "/api/v1/stocks/{t}/dashboard"


def test_empty_state_returns_contract_shape(web_client):
    """Fresh install: no snapshot yet — all 4 keys present, unavailable."""
    r = web_client.get(DASH.format(t="2330.TW"))
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "2330.TW"
    assert body["rec_date"] is None
    assert body["recommendation"] is None
    assert set(body["modules"].keys()) == set(SCORING_MODULES)
    for m in SCORING_MODULES:
        assert body["modules"][m]["status"] == "unavailable"
    assert "not financial advice" in body["disclaimer"].lower()  # FR-39


def test_full_snapshot_has_all_modules_and_breakdown(web_client, store):
    store["recommendations"].append(make_recommendation(ticker_id=1))
    r = web_client.get(DASH.format(t="2330.TW"))
    assert r.status_code == 200
    body = r.json()
    rec = body["recommendation"]
    assert rec["composite_call"] == "BUY"
    assert len(rec["per_module_breakdown"]) >= 1  # never ships without reasoning
    assert set(body["modules"].keys()) == set(SCORING_MODULES)
    assert all(body["modules"][m]["status"] == "ok" for m in SCORING_MODULES)


def test_single_module_down_is_200_with_flag(web_client, store):
    """Deck §22.4: one adapter fails → 200, module flagged, weights renormalised."""
    store["recommendations"].append(make_recommendation(ticker_id=1, unavailable=("news",)))
    r = web_client.get(DASH.format(t="2330.TW"))
    assert r.status_code == 200
    body = r.json()
    assert body["modules"]["news"]["status"] == "unavailable"
    rec = body["recommendation"]
    assert rec["reduced_confidence"] is True
    assert rec["data_completeness"] == 0.75
    news = next(b for b in rec["per_module_breakdown"] if b["module"] == "news")
    assert news["weight_effective"] == 0.0
    live_effective = sum(
        b["weight_effective"] for b in rec["per_module_breakdown"] if b["status"] == "ok"
    )
    # Renormalised; tolerance covers domain-contract v1.2.2 §9's 4dp rounding.
    assert abs(live_effective - 1.0) <= 0.0002


def test_two_modules_down_is_suppressed_analysis_only(web_client, store):
    """>=2 scoring modules down → call suppressed, never silent."""
    store["recommendations"].append(
        make_recommendation(ticker_id=1, unavailable=("news", "chip"))
    )
    r = web_client.get(DASH.format(t="2330.TW"))
    assert r.status_code == 200
    rec = r.json()["recommendation"]
    assert rec["composite_call"] == "SUPPRESSED"
    assert rec["suppressed_reason"] == "Analysis Only — Insufficient Data"
    # ck_rec_suppressed_shape: a suppressed row carries NO score/target/confidence.
    assert rec["composite_signal"] is None
    assert rec["target_price"] is None
    assert rec["confidence_level"] is None
    assert rec["per_module_breakdown"]  # transparency: breakdown still present


def test_uncovered_ticker_is_404_sector_not_covered(web_client):
    r = web_client.get(DASH.format(t="XXXX.TW"))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SECTOR_NOT_COVERED"


def test_unknown_ticker_is_404_sector_not_covered(web_client):
    r = web_client.get(DASH.format(t="NOPE"))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SECTOR_NOT_COVERED"


def test_disclaimer_header_on_every_response(web_client):
    """FR-39: disclaimer accompanies every API response."""
    r = web_client.get("/healthz")
    assert "not financial advice" in r.headers["X-Disclaimer"].lower()
