"""Task #13 backtest engine — unit + integration + endpoint contract tests.

Covers: add_months clamping, segment routing, strict ±2.0pp edges,
run_backtest over a fake conn (hand-computed rolling accuracy fractions,
full/partial segregation, SELL sign convention, maturity + windowing, upsert
idempotency, <12mo gate), the pipeline stage statuses, the is_covered
regression (benchmarks never scored), and the GET /stocks/{ticker}/backtest
contract shape.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.services.backtest_engine import (
    add_months,
    benchmark_for,
    delta_pp,
    evaluate_call,
    run_backtest,
    segment_for,
)

AS_OF = date(2026, 7, 8)


# --- pure core ----------------------------------------------------------------

@pytest.mark.parametrize("start,months,expected", [
    (date(2026, 1, 31), 1, date(2026, 2, 28)),   # clamp: Jan 31 + 1mo
    (date(2024, 1, 31), 1, date(2024, 2, 29)),   # leap year clamp
    (date(2026, 3, 31), -1, date(2026, 2, 28)),  # clamp going backwards
    (date(2026, 7, 8), -12, date(2025, 7, 8)),   # plain year hop
    (date(2025, 12, 15), 1, date(2026, 1, 15)),  # year rollover forward
    (date(2026, 1, 15), -3, date(2025, 10, 15)),  # year rollover backward
])
def test_add_months(start, months, expected):
    assert add_months(start, months) == expected


def test_segment_for_full_iff_exactly_one():
    assert segment_for(Decimal("1.000")) == "full"
    assert segment_for(Decimal("0.750")) == "partial"
    assert segment_for(Decimal("0.999")) == "partial"
    assert segment_for(None) == "partial"  # unknown is never presented as full


def test_benchmark_for_tpex_suffix():
    assert benchmark_for("6488.TWO") == "^TWII"  # TPEx rides the TW index too
    assert benchmark_for("2330.TW") == "^TWII"
    assert benchmark_for("AAPL") == "^GSPC"


def test_evaluate_call_strict_edges_and_calls():
    # SELL at exactly -2.0pp is NOT correct (strict <, mirror of GF-BT-buy-edge)
    assert evaluate_call("SELL", Decimal("3.0"), Decimal("5.0")) is False
    assert evaluate_call("SELL", Decimal("2.9"), Decimal("5.0")) is True
    # STRONG_* share the directional rule
    assert evaluate_call("STRONG_BUY", Decimal("8.0"), Decimal("5.0")) is True
    assert evaluate_call("STRONG_SELL", Decimal("1.0"), Decimal("5.0")) is True
    # SUPPRESSED is excluded exactly like HOLD
    assert evaluate_call("SUPPRESSED", Decimal("9.0"), Decimal("0.0")) is None
    assert delta_pp(Decimal("8.0"), Decimal("5.0")) == Decimal("3.0")


# --- integration: run_backtest over a fake conn --------------------------------

class FakeBacktestDb:
    """Emulates the four queries run_backtest/compute_ticker_backtest make and
    captures backtest_result upserts keyed by the schema's conflict key."""

    def __init__(self, tickers, recs, bars, earliest=None):
        self.tickers = tickers          # [{"id", "full_symbol"}]
        self.recs = recs                # rec dicts incl full_symbol
        self.bars = bars                # [(ticker_id, bar_date, close)]
        self.earliest = earliest        # gate override (defaults to min rec)
        self.upserts: dict[tuple, tuple] = {}

    async def fetchval(self, query, *args):
        assert "min(rec_date)" in query.lower()
        if self.earliest is not None:
            return self.earliest
        return min((r["rec_date"] for r in self.recs), default=None)

    async def fetch(self, query, *args):
        q = " ".join(query.split()).lower()
        if "from price_bar" in q:
            rows = [{"ticker_id": t, "bar_date": d, "close": c}
                    for (t, d, c) in self.bars if d <= args[0]]
            # emulate the query's ORDER BY ticker_id, bar_date
            return sorted(rows, key=lambda r: (r["ticker_id"], r["bar_date"]))
        if "from recommendation" in q:
            rows = [r for r in self.recs if args[0] <= r["rec_date"] <= args[1]]
            if len(args) > 2:
                rows = [r for r in rows if r["ticker_id"] == args[2]]
            return rows
        if "from ticker" in q:
            return self.tickers
        raise AssertionError(f"unhandled query: {q[:120]}")

    async def execute(self, query, *args):
        q = " ".join(query.split()).lower()
        assert ("on conflict (as_of_date, window_months, completeness_segment,"
                " methodology_version)" in q)  # same-day recompute only
        as_of, window, segment, accuracy, estimated, n, version = args
        self.upserts[(as_of, window, segment, version)] = (accuracy, estimated, n)
        return "INSERT 0 1"


def _rec(ticker_id, rec_date, call, *, horizon=3, dc="1.000", full_symbol=None):
    return {
        "ticker_id": ticker_id, "rec_date": rec_date, "composite_call": call,
        "horizon_months": horizon, "data_completeness": Decimal(dc),
        "full_symbol": full_symbol or f"T{ticker_id}",
    }


D_ = Decimal

TICKERS = [
    {"id": 1, "full_symbol": "T1"}, {"id": 2, "full_symbol": "T2"},
    {"id": 3, "full_symbol": "T3"}, {"id": 5, "full_symbol": "T5"},
    {"id": 6, "full_symbol": "T6"},
    {"id": 90, "full_symbol": "^GSPC"},
]

# Hand-computed scenario (as_of 2026-07-08, horizon 3mo, all US -> ^GSPC):
# r1 BUY  +10% vs +5%  -> delta +5 -> correct   (full)
# r2 BUY   +2% vs +1%  -> delta +1 -> incorrect (full)
# r3 SELL  +1% vs +5%  -> delta -4 -> correct   (full; signed +4)
# r4 HOLD              -> excluded from accuracy entirely
# r5 BUY  +10% vs +5%  -> delta +5 -> correct   (partial dc=0.750)
# r6 BUY dated 2026-06-01 -> exit 2026-09-01 > as_of -> immature
# r7 BUY on T6 (no bars)  -> unpriced
RECS = [
    _rec(1, date(2026, 2, 2), "BUY"),
    _rec(2, date(2026, 2, 16), "BUY"),
    _rec(3, date(2026, 3, 2), "SELL"),
    _rec(1, date(2026, 3, 16), "HOLD"),
    _rec(5, date(2026, 3, 23), "BUY", dc="0.750"),
    _rec(1, date(2026, 6, 1), "BUY"),
    _rec(6, date(2026, 2, 2), "BUY"),
]

BARS = [
    (1, date(2026, 2, 2), D_("100")), (1, date(2026, 5, 2), D_("110")),
    (2, date(2026, 2, 16), D_("100")), (2, date(2026, 5, 16), D_("102")),
    (3, date(2026, 3, 2), D_("100")), (3, date(2026, 6, 2), D_("101")),
    (5, date(2026, 3, 23), D_("100")), (5, date(2026, 6, 23), D_("110")),
    # ^GSPC: +5% for r1/r3/r5 horizons, +1% for r2's
    (90, date(2026, 2, 2), D_("1000")), (90, date(2026, 5, 2), D_("1050")),
    (90, date(2026, 2, 16), D_("2000")), (90, date(2026, 5, 16), D_("2020")),
    (90, date(2026, 3, 2), D_("1000")), (90, date(2026, 6, 2), D_("1050")),
    (90, date(2026, 3, 23), D_("4000")), (90, date(2026, 6, 23), D_("4200")),
]

SUFFICIENT_EARLIEST = date(2024, 6, 1)  # >=12mo before as_of


def make_db(earliest=SUFFICIENT_EARLIEST):
    return FakeBacktestDb(TICKERS, RECS, BARS, earliest=earliest)


async def test_run_backtest_rolling_accuracy_and_segments():
    db = make_db()
    stats = await run_backtest(db, AS_OF)

    assert stats.insufficient_history is False
    assert stats.recs_seen == 7 and stats.recs_evaluated == 4
    assert stats.recs_excluded == 1    # HOLD — never correct nor incorrect
    assert stats.recs_immature == 1    # no peeking past as_of
    assert stats.recs_unpriced == 1    # missing bars: dropped, counted
    assert stats.rows_upserted == 6    # 3 windows x 2 segments

    # window 6, full segment: 2 correct of 3 -> 0.6667 (4dp fraction);
    # estimated_return = mean(+5, +1, +4) = 3.3333 — the SELL that lagged
    # contributes POSITIVELY (sign convention).
    acc, est, n = db.upserts[(AS_OF, 6, "full", "mvp-1.0")]
    assert (acc, est, n) == (Decimal("0.6667"), Decimal("3.3333"), 3)
    # partial NEVER blends into full: its lone correct rec sits alone
    acc, est, n = db.upserts[(AS_OF, 6, "partial", "mvp-1.0")]
    assert (acc, est, n) == (Decimal("1.0000"), Decimal("5.0000"), 1)
    # window 3 (start 2026-04-08): nothing matured inside -> honest empties
    assert db.upserts[(AS_OF, 3, "full", "mvp-1.0")] == (None, None, 0)
    assert db.upserts[(AS_OF, 3, "partial", "mvp-1.0")] == (None, None, 0)
    # window 12 sees the same evaluated population here
    assert db.upserts[(AS_OF, 12, "full", "mvp-1.0")] == (
        Decimal("0.6667"), Decimal("3.3333"), 3)


async def test_run_backtest_upsert_idempotent():
    db = make_db()
    await run_backtest(db, AS_OF)
    first = dict(db.upserts)
    stats = await run_backtest(db, AS_OF)  # same-day recompute
    assert db.upserts == first             # same keys, same rows
    assert len(db.upserts) == 6
    assert stats.rows_upserted == 6


async def test_run_backtest_history_gate_nulls_numbers_keeps_samples():
    db = make_db(earliest=date(2025, 10, 1))  # 9mo of history -> gated
    stats = await run_backtest(db, AS_OF)
    assert stats.insufficient_history is True
    acc, est, n = db.upserts[(AS_OF, 6, "full", "mvp-1.0")]
    assert acc is None and est is None  # no misleading number
    assert n == 3                       # sample_size still reported


async def test_run_backtest_no_history_writes_nothing():
    db = FakeBacktestDb(TICKERS, [], BARS)
    stats = await run_backtest(db, AS_OF)
    assert stats.rows_upserted == 0 and db.upserts == {}


# --- pipeline stage -------------------------------------------------------------

async def test_backtest_stage_statuses():
    from app.batch.pipeline import BACKTEST_SOURCE, KNOWN_SOURCES, _run_backtest_stage

    assert BACKTEST_SOURCE == "backtest"
    assert BACKTEST_SOURCE not in KNOWN_SOURCES  # derived stage, not a source

    status, message = await _run_backtest_stage(make_db(), AS_OF)
    assert status == "ok" and "evaluated=4" in message

    status, message = await _run_backtest_stage(
        FakeBacktestDb(TICKERS, [], BARS), AS_OF)
    assert status == "unavailable" and "no recommendation history" in message


# --- regression: benchmarks (is_covered=false) are never scored ------------------

async def test_benchmarks_never_get_recommendation_rows(monkeypatch):
    """run_engine must keep filtering on is_covered — a benchmark row with
    plenty of price bars still gets NO recommendation."""
    import app.services.recommendation_engine as engine

    scored: list[str] = []

    async def fake_score_ticker(conn, ticker, *args, **kwargs):
        scored.append(ticker["full_symbol"])
        return None

    monkeypatch.setattr(engine, "score_ticker", fake_score_ticker)

    tickers = [
        {"id": 1, "full_symbol": "AAPL", "exchange": "US", "is_covered": True},
        {"id": 90, "full_symbol": "^GSPC", "exchange": "US", "is_covered": False},
    ]

    class Db:
        async def fetchrow(self, query, *args):
            return None  # user_config absent -> engine defaults

        async def fetch(self, query, *args):
            # emulate SQL: rows survive only if the query keeps the filter
            if "is_covered" in query:
                return [t for t in tickers if t["is_covered"]]
            return tickers

    await engine.run_engine(Db(), AS_OF)
    assert scored == ["AAPL"]  # the benchmark was never even considered


# --- endpoint contract: GET /stocks/{ticker}/backtest ----------------------------

BT = "/api/v1/stocks/{t}/backtest"


def test_endpoint_shape_fresh_data(web_client):
    r = web_client.get(BT.format(t="2330.TW"))
    assert r.status_code == 200
    body = r.json()
    assert body["window_months"] == 6  # default
    assert body["insufficient_history"] is True  # fresh data: honest gate
    assert body["rolling_accuracy_full"] is None
    assert body["rolling_accuracy_partial"] is None
    assert body["estimated_return"] is None
    assert body["benchmark"] == "^TWII"  # market-matched label
    assert body["methodology_version"] == "mvp-1.0"
    # FR-39 disclaimer fields ride every backtest payload
    assert "not personalized investment advice" in body["disclaimer"].lower()
    assert body["disclaimer_version"] == "fr39-v1"


def test_endpoint_us_benchmark_and_window_param(web_client):
    r = web_client.get(BT.format(t="AAPL"), params={"window_months": 12})
    assert r.status_code == 200
    assert r.json()["benchmark"] == "^GSPC"
    assert r.json()["window_months"] == 12


def test_endpoint_window_months_validated(web_client):
    assert web_client.get(BT.format(t="AAPL"), params={"window_months": 4}).status_code == 422


def test_endpoint_unknown_ticker_404(web_client):
    r = web_client.get(BT.format(t="NOPE"))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SECTOR_NOT_COVERED"


def test_endpoint_uncovered_ticker_404(web_client):
    r = web_client.get(BT.format(t="XXXX.TW"))  # exists but is_covered=false
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SECTOR_NOT_COVERED"


def test_endpoint_serves_computed_numbers(web_client, store, monkeypatch):
    """With matured recs + bars in the store, the endpoint reports the same
    §10 numbers as the pure core (here: one correct BUY, full segment)."""
    from tests.conftest import make_recommendation

    rec = make_recommendation(ticker_id=1)
    rec["rec_date"] = date(2026, 2, 2)
    rec["horizon_months"] = 3
    store["recommendations"].append(rec)
    # earliest-history row >=12mo back (any ticker) opens the gate honestly
    old = make_recommendation(ticker_id=2)
    old["rec_date"] = date(2024, 6, 3)
    store["recommendations"].append(old)
    store["tickers"]["^TWII"] = {
        "id": 91, "full_symbol": "^TWII", "symbol": "^TWII", "exchange": "TWSE",
        "name": "TAIEX (TW benchmark index)", "sector": None, "is_covered": False,
    }
    store["price_bars"] = [
        {"ticker_id": 1, "bar_date": date(2026, 2, 2), "close": Decimal("100")},
        {"ticker_id": 1, "bar_date": date(2026, 5, 2), "close": Decimal("110")},
        {"ticker_id": 91, "bar_date": date(2026, 2, 2), "close": Decimal("20000")},
        {"ticker_id": 91, "bar_date": date(2026, 5, 2), "close": Decimal("21000")},
    ]

    import app.services.backtest_engine as bt

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return AS_OF

    monkeypatch.setattr(bt, "date", _FrozenDate)
    r = web_client.get(BT.format(t="2330.TW"))
    assert r.status_code == 200
    body = r.json()
    assert body["insufficient_history"] is False
    assert body["rolling_accuracy_full"] == 1.0     # BUY beat ^TWII by +5pp
    assert body["rolling_accuracy_partial"] is None  # no partial samples
    assert body["estimated_return"] == 5.0
    assert body["benchmark"] == "^TWII"
