"""Task #13 backtest engine vs the A6 golden fixtures (docs/qa/golden_fixtures.json).

Data-driven: EVERY D5_backtest case runs through the pure backtest core and
must reproduce the expect block exactly (same Cindy ruling as D1: any
divergence is a defect). Case shapes:
- call+returns   -> delta_pp + evaluate_call (strict ±2.0pp, GF-BT-buy-edge);
- day_type       -> segment routing: partial NEVER blends into the full bucket;
- history_months -> the <12mo gate nulls the headline number;
- markets        -> benchmark matched to the ticker's market (§10).
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.backtest_engine import (
    EvaluatedRec,
    add_months,
    benchmark_for,
    delta_pp,
    evaluate_call,
    history_insufficient,
    segment_for,
    summarize_segment,
)

FIXTURES = json.loads(
    (Path(__file__).resolve().parent.parent / "docs" / "qa" / "golden_fixtures.json")
    .read_text()
)
D5_CASES = FIXTURES["D5_backtest"]

AS_OF = date(2026, 7, 8)


def D(value) -> Decimal:  # noqa: N802 - fixture-literal helper
    return Decimal(str(value))


def _sample_rec(segment: str) -> EvaluatedRec:
    """A correct, evaluated rec used to probe bucket routing / gating."""
    return EvaluatedRec(rec_date=date(2026, 3, 2), segment=segment,
                        correct=True, signed_delta_pp=Decimal("3.0"))


@pytest.mark.parametrize("case", D5_CASES, ids=[c["id"] for c in D5_CASES])
def test_d5_golden_case(case: dict) -> None:
    inp, expect = case["input"], case["expect"]

    if "call" in inp:
        stock, bench = D(inp["stock_return_pct"]), D(inp["benchmark_return_pct"])
        if "delta_pp" in expect:
            assert float(delta_pp(stock, bench)) == expect["delta_pp"]
        outcome = evaluate_call(inp["call"], stock, bench)
        if "correct" in expect:
            assert outcome is expect["correct"]
        if expect.get("counted_in_accuracy") is False:
            # HOLD/SUPPRESSED: None — neither correct nor incorrect (§10)
            assert outcome is None

    elif "day_type" in inp:
        # partial-data day: routed by data_completeness < 1.000, and its
        # evaluated rec must land ONLY in the partial bucket — never blended.
        rec = _sample_rec(segment_for(Decimal("0.750")))
        full = summarize_segment([e for e in [rec] if e.segment == "full"], False)
        partial = summarize_segment([e for e in [rec] if e.segment == "partial"], False)
        assert (full.sample_size > 0) is expect["in_rolling_accuracy_full"]
        assert (partial.sample_size > 0) is expect["in_rolling_accuracy_partial"]
        assert full.rolling_accuracy is None  # nothing leaked into the headline

    elif "history_months" in inp:
        earliest = add_months(AS_OF, -inp["history_months"])
        insufficient = history_insufficient(earliest, AS_OF)
        assert insufficient is expect["insufficient_history"]
        # even with correct samples on hand, the gate nulls the number
        gated = summarize_segment([_sample_rec("full")], insufficient)
        assert gated.rolling_accuracy == expect["rolling_accuracy_full"]  # null
        assert gated.estimated_return is None
        assert gated.sample_size == 1  # sample_size may still be reported

    else:
        assert "markets" in inp, f"unrecognised fixture shape: {case['id']}"
        for symbol, expected_benchmark in inp["markets"].items():
            assert benchmark_for(symbol) == expected_benchmark
            assert benchmark_for(symbol) == expect[symbol]
