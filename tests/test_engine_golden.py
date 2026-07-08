"""Task #10 engine vs the A6 golden fixtures (docs/qa/golden_fixtures.json).

Data-driven: EVERY D1_scoring case runs through the pure engine core and must
reproduce the expect block exactly (Cindy ruling 2026-07-08: any divergence is
a defect). D5_backtest is task #13 — not loaded here.

Plus contract-shape tests: breakdown always lists 4 modules, suppressed rows
match ck_rec_suppressed_shape, renormalisation for each single-module outage
(TC-DEG-01b), full-precision-then-round (GF-B raw 0.771428… → 0.77), and the
persist wrapper's Decimal/JSONB binds.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.recommendation_engine import (
    MODULES,
    SUPPRESSED_REASON,
    TargetInputs,
    call_for_composite,
    compute_recommendation,
    compute_targets,
    confidence_for,
    persist_recommendation,
)

FIXTURES = json.loads(
    (Path(__file__).resolve().parent.parent / "docs" / "qa" / "golden_fixtures.json")
    .read_text()
)
D1_CASES = FIXTURES["D1_scoring"]


def D(value) -> Decimal | None:  # noqa: N802 - fixture-literal helper
    return None if value is None else Decimal(str(value))


def _case_kind(case: dict) -> str:
    inp = case["input"]
    if "signals" in inp:
        return "full"
    if "available_signals" in inp:
        return "confidence"
    if "latest_close" in inp:
        return "target"
    assert "composite_signal" in inp, f"unrecognised fixture shape: {case['id']}"
    return "band"


def _run_full_case(inp: dict):
    signals = {
        m: (D(inp["signals"][m]) if m in inp["available"] else None) for m in MODULES
    }
    target_inputs = None
    if "target_inputs" in inp:
        ti = inp["target_inputs"]
        target_inputs = TargetInputs(
            latest_close=D(ti.get("latest_close")),
            peer_median_pe=D(ti.get("peer_median_PE")),
            trailing_eps=D(ti.get("trailing_EPS")),
        )
    kwargs = {}
    # GF-CHIP-PARTIAL (v1.2.6 §9): chip sub-field gaps flow in as
    # subfields_complete=false + note — availability/renorm untouched.
    sub = inp.get("chip_subfield_status")
    if sub and any(v is None for v in sub.values()):
        kwargs["subfields_complete"] = {"chip": False}
        kwargs["notes"] = {
            "chip": "3-institution nets only; margin/block unavailable"}
    return compute_recommendation(signals, target_inputs=target_inputs, **kwargs)


def _assert_full_case(case: dict) -> None:
    result = _run_full_case(case["input"])
    for key, expected in case["expect"].items():
        if key == "note":
            continue
        if key == "weight_effective":
            got = {m: float(result.weight_effective[m]) for m in expected}
            assert got == expected, (key, got, expected)
        elif key == "per_module_breakdown_len":
            assert len(result.per_module_breakdown) == expected
        elif key in ("composite_signal", "data_completeness", "spread",
                     "confidence_pct", "target_base", "target_bear", "target_bull"):
            got_dec = getattr(result, key)
            got = None if got_dec is None else float(got_dec)
            assert got == expected, (key, got, expected)
        elif key.startswith("per_module_breakdown."):
            _, module, field = key.split(".", 2)
            item = next(b for b in result.per_module_breakdown
                        if b["module"] == module)
            if field.endswith("_nonempty"):
                assert bool(item.get(field.removesuffix("_nonempty"))), key
            else:
                assert item.get(field) == expected, (key, item.get(field))
        elif key == "chip_signal_null":
            chip = next(b for b in result.per_module_breakdown
                        if b["module"] == "chip")
            assert (chip["signal_score"] is None) == expected, key
        elif key == "renormalisation_triggered":
            assert result.reduced_confidence == expected, key
        else:
            assert getattr(result, key) == expected, (key, expected)


@pytest.mark.parametrize("case", D1_CASES, ids=[c["id"] for c in D1_CASES])
def test_d1_golden_case(case: dict) -> None:
    kind = _case_kind(case)
    inp, expect = case["input"], case["expect"]

    if kind == "full":
        _assert_full_case(case)

    elif kind == "band":
        assert call_for_composite(D(inp["composite_signal"])) == expect["composite_call"]

    elif kind == "confidence":
        composite = D(inp.get("composite_signal", inp.get("composite_sign", 0)))
        pct, level = confidence_for(
            inp["composite_call"], composite,
            [D(s) for s in inp["available_signals"]],
        )
        assert float(pct) == expect["confidence_pct"]
        assert level == expect["confidence_level"]

    else:  # target
        base, bear, bull, usable = compute_targets(
            TargetInputs(
                latest_close=D(inp.get("latest_close")),
                peer_median_pe=D(inp.get("peer_median_PE")),
                trailing_eps=D(inp.get("trailing_EPS")),
            ),
            fundamental_available=inp.get("fundamental_scoring_available", True),
        )
        assert usable == expect["target_fundamental_usable"]
        assert float(base) == expect["target_base"]
        if "target_bear" in expect:
            assert float(bear) == expect["target_bear"]
            assert float(bull) == expect["target_bull"]
        # §8: an unusable fundamental leg carries Reduced-Confidence framing.
        if expect.get("reduced_confidence_framing") or expect.get("reduced_confidence"):
            assert usable is False


# --- band boundary hardening: both sides of every edge (TC-BAND-EXACT) --------

@pytest.mark.parametrize("value,call", [
    ("-2.00", "STRONG_SELL"), ("-1.501", "STRONG_SELL"),
    ("-1.50", "SELL"), ("-1.499", "SELL"), ("-0.751", "SELL"),
    ("-0.75", "HOLD"), ("-0.749", "HOLD"), ("0", "HOLD"),
    ("0.749", "HOLD"), ("0.75", "HOLD"),
    ("0.751", "BUY"), ("1.499", "BUY"), ("1.50", "BUY"),
    ("1.501", "STRONG_BUY"), ("2.00", "STRONG_BUY"),
])
def test_band_boundary_milder_wins(value: str, call: str) -> None:
    assert call_for_composite(Decimal(value)) == call


# --- breakdown shape (FR-37 / contract §9) -------------------------------------

FULL_CASES = [c for c in D1_CASES if _case_kind(c) == "full"]


@pytest.mark.parametrize("case", FULL_CASES, ids=[c["id"] for c in FULL_CASES])
def test_breakdown_always_lists_all_four_modules(case: dict) -> None:
    result = _run_full_case(case["input"])
    breakdown = result.per_module_breakdown
    assert [b["module"] for b in breakdown] == list(MODULES)  # fixed order
    for b in breakdown:
        assert set(b) >= {"module", "signal_score", "weight_assigned",
                          "weight_effective", "status"}
        if b["status"] == "unavailable":  # §9: null score, 0.0000 effective
            assert b["signal_score"] is None
            assert b["weight_effective"] == 0.0


# --- TC-DEG-01b: each module individually down -> renormalisation correct ------

@pytest.mark.parametrize("down", MODULES)
def test_single_module_down_renormalises(down: str) -> None:
    signals = {"technical": Decimal("1.2"), "fundamental": Decimal("0.8"),
               "chip": Decimal("1.5"), "news": Decimal("-0.5")}
    signals[down] = None
    result = compute_recommendation(signals)
    assert result.reduced_confidence is True
    assert result.data_completeness == Decimal("0.75")
    assert result.weight_effective[down] == Decimal("0.0000")
    survivors_sum = sum(v for m, v in result.weight_effective.items() if m != down)
    assert abs(survivors_sum - 1) <= Decimal("0.0002")  # 4dp storage rounding
    assert result.composite_call != "SUPPRESSED"


# --- suppressed shape matches ck_rec_suppressed_shape ---------------------------

def test_suppressed_shape_nulls() -> None:
    result = compute_recommendation({"fundamental": Decimal("0.8"),
                                     "news": Decimal("-0.5")})
    assert result.composite_call == "SUPPRESSED"
    # ck_rec_suppressed_shape: composite/targets/confidence all NULL
    assert result.composite_signal is None
    assert result.target_base is None
    assert result.target_bear is None
    assert result.target_bull is None
    assert result.confidence_level is None
    assert result.confidence_pct is None
    assert result.suppressed_reason == SUPPRESSED_REASON
    assert result.conflict_flag is False  # NOT NULL column keeps a value
    assert len(result.per_module_breakdown) == 4
    statuses = {b["module"]: b["status"] for b in result.per_module_breakdown}
    assert statuses == {"technical": "unavailable", "fundamental": "ok",
                        "chip": "unavailable", "news": "ok"}


def test_zero_available_is_still_suppressed_shape() -> None:
    result = compute_recommendation(dict.fromkeys(MODULES))
    assert result.composite_call == "SUPPRESSED"
    assert result.data_completeness == Decimal("0")


# --- full precision then round (contract §2/§3, GF-B) ---------------------------

def test_full_precision_compute_rounds_only_composite() -> None:
    """State B: composite raw 0.540/0.70 = 0.771428… — rounding intermediate
    weights to 4dp (0.4286·0.8 + 0.3571·1.5 + 0.2143·(−0.5)) would give
    0.77140 too, but 2dp weights (0.43/0.36/0.21) would drift to 0.779.
    The engine must land exactly on 0.77 from FULL-precision weights."""
    result = compute_recommendation({
        "technical": None, "fundamental": Decimal("0.8"),
        "chip": Decimal("1.5"), "news": Decimal("-0.5"),
    })
    assert result.composite_signal == Decimal("0.77")  # persisted 2dp, half-up
    assert result.composite_call == "BUY"  # 0.77 > +0.75 — just above the edge
    # stored effective weights are the 4dp canonical values
    assert result.weight_effective == {
        "technical": Decimal("0.0000"), "fundamental": Decimal("0.4286"),
        "chip": Decimal("0.3571"), "news": Decimal("0.2143"),
    }


def test_half_up_rounding_at_persistence() -> None:
    # 0.30·0.425·... simpler: craft composite raw 0.005 -> half-up 0.01
    result = compute_recommendation({
        "technical": Decimal("0.005"), "fundamental": Decimal("0.005"),
        "chip": Decimal("0.005"), "news": Decimal("0.005"),
    })
    assert result.composite_signal == Decimal("0.01")  # banker's would give 0.00


# --- persist wrapper: Decimal binds + JSONB + same-day idempotency --------------

class _CaptureConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args):
        self.calls.append((query, args))
        return "INSERT 0 1"


async def test_persist_scored_row_binds() -> None:
    conn = _CaptureConn()
    result = compute_recommendation(
        {"technical": Decimal("1.2"), "fundamental": Decimal("0.8"),
         "chip": Decimal("1.5"), "news": Decimal("-0.5")},
        target_inputs=TargetInputs(latest_close=Decimal("900"),
                                   peer_median_pe=Decimal("18"),
                                   trailing_eps=Decimal("40")),
    )
    await persist_recommendation(conn, ticker_id=1, rec_date=date(2026, 7, 8),
                                 result=result, horizon_months=6,
                                 methodology_version="mvp-1.0")
    query, args = conn.calls[0]
    # §10/ADR-003: immutable history — conflict key includes rec_date, so a
    # re-run can only overwrite the SAME day, never a prior row.
    assert "ON CONFLICT (ticker_id, rec_date) DO UPDATE" in query
    (tid, rec_date, composite, call, base, bear, bull, level, pct, conflict,
     horizon, breakdown, completeness, reduced, version) = args
    assert composite == Decimal("0.90") and isinstance(composite, Decimal)
    assert (base, bear, bull) == (Decimal("810.0000"), Decimal("688.5000"),
                                  Decimal("931.5000"))
    assert isinstance(base, Decimal)
    assert pct == Decimal("75.00") and level == "HIGH"
    assert completeness == Decimal("1.000")
    assert version == "mvp-1.0" and horizon == 6
    parsed = json.loads(breakdown)  # JSONB bound as json.dumps text
    assert [b["module"] for b in parsed] == list(MODULES)


async def test_persist_suppressed_row_binds_nulls() -> None:
    conn = _CaptureConn()
    result = compute_recommendation({"fundamental": Decimal("0.8"),
                                     "news": Decimal("-0.5")})
    await persist_recommendation(conn, ticker_id=1, rec_date=date(2026, 7, 8),
                                 result=result, horizon_months=6,
                                 methodology_version="mvp-1.0")
    _, args = conn.calls[0]
    composite, call = args[2], args[3]
    base, bear, bull, level, pct = args[4], args[5], args[6], args[7], args[8]
    assert call == "SUPPRESSED"
    assert (composite, base, bear, bull, level, pct) == (None,) * 6
    assert args[12] == Decimal("0.500")  # data_completeness NUMERIC(4,3)


def test_gf_chip_partial_intra_module_binding():
    """GF-CHIP-PARTIAL-intra-module (A6, v1.2.6 §9): all 4 modules present,
    chip scored from nets with margin/block NULL — chip stays ok/in-composite,
    NO renormalisation, module-level completeness unchanged, partial DISCLOSED."""
    from decimal import Decimal

    from app.services.recommendation_engine import compute_recommendation

    result = compute_recommendation(
        {"technical": Decimal("1.2"), "fundamental": Decimal("0.8"),
         "chip": Decimal("1.5"), "news": Decimal("-0.5")},
        notes={"chip": "3-institution nets only; margin/block unavailable"},
        subfields_complete={"chip": False},
    )
    chip = next(b for b in result.per_module_breakdown if b["module"] == "chip")
    assert chip["status"] == "ok"                      # NOT unavailable
    assert chip["subfields_complete"] is False         # partial DISCLOSED
    assert "margin/block unavailable" in chip["subfields_note"]
    assert chip["signal_score"] is not None
    assert float(result.data_completeness) == 1.00     # module-level unchanged
    assert result.reduced_confidence is False          # no renormalisation
    assert chip["weight_effective"] == 0.25            # weight untouched
    # other modules default true
    tech = next(b for b in result.per_module_breakdown if b["module"] == "technical")
    assert tech["subfields_complete"] is True
