"""Recommendation engine (task #10) — domain-contract v1.2.5 §1–§10.

Pure deterministic core (`compute_recommendation`) + a persist wrapper
(`persist_recommendation`) + the per-ticker batch orchestration (`run_engine`)
the pipeline calls after the ingestion sources.

Precision discipline (contract §2/§3, GF-B): ALL composite math runs at full
Decimal precision — effective weights are never rounded before the composite;
rounding happens once, at the persistence edge (composite 2dp half-up,
module scores 2dp, weight_effective 4dp, targets 4dp).

Semantics implemented exactly per contract:
- §2  renormalisation over available modules; data_completeness = |A|/4.
- §3  composite = Σ w_eff·signal, rounded 2dp half-up; the CALL is banded on
      the ROUNDED composite (GF-B: raw 0.771428… → 0.77 → BUY; GF-D note:
      persisted score and glyph must agree).
- §4  0 down → normal; 1 down → renormalise + reduced_confidence; ≥2 down →
      SUPPRESS (no score/target/confidence; reason fixed; breakdown still
      lists all 4 modules).
- §5  bands SS[-2,-1.5) S[-1.5,-0.75) HOLD[-0.75,+0.75] BUY(+0.75,+1.5]
      SB(+1.5,+2] — boundary → milder call.
- §6  FR-28 confidence (A1-ratified): HOLD call → agree = |signal| ≤ 0.75;
      directional call → sign match (a 0 signal does NOT agree).
- §7  conflict when spread over surviving signals is STRICTLY > 2.0.
- §8  FR-27 target: usable = fundamental-available AND EPS>0 AND peer PE
      non-null; usable → 0.5·(PE·EPS)+0.5·close else close-only with
      Reduced-Confidence framing. bear/bull = base·0.85/1.15, 4dp.
- §9  per_module_breakdown: always all 4 modules, fixed order, frozen keys
      (+ an additive optional `note` for visible partial-completeness — the
      read path's pydantic model ignores unknown keys).
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.batch.signals import ModuleSignal, q2
from app.batch.signals.chip import chip_signal
from app.batch.signals.fundamental import fundamental_signal
from app.batch.signals.news import news_signal
from app.batch.signals.technical import technical_signal
from app.core.config import get_settings

logger = logging.getLogger(__name__)

MODULES = ("technical", "fundamental", "chip", "news")
DEFAULT_WEIGHTS = {
    "technical": Decimal("0.30"), "fundamental": Decimal("0.30"),
    "chip": Decimal("0.25"), "news": Decimal("0.15"),
}
SUPPRESSED_REASON = "Analysis Only — Insufficient Data"

_HOLD_EDGE = Decimal("0.75")      # contract §5: HOLD owns both edges
_STRONG_EDGE = Decimal("1.5")     # boundary -> milder call
_CONFLICT_SPREAD = Decimal("2.0")  # §7: strictly greater
_TARGET_BEAR = Decimal("0.85")
_TARGET_BULL = Decimal("1.15")
_TARGET_W_FUNDAMENTAL = Decimal("0.5")  # §8 blend, methodology-versioned
_TARGET_W_TECHNICAL = Decimal("0.5")
_CONF_HIGH = Decimal("75")
_CONF_MEDIUM = Decimal("50")


def _q4(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def call_for_composite(composite: Decimal) -> str:
    """Band mapping (§5) on the rounded composite; boundaries → milder call
    (Hold owns ±0.75; exactly ±1.5 stays with the regular call)."""
    if composite < -_STRONG_EDGE:
        return "STRONG_SELL"
    if composite < -_HOLD_EDGE:
        return "SELL"
    if composite <= _HOLD_EDGE:
        return "HOLD"
    if composite <= _STRONG_EDGE:
        return "BUY"
    return "STRONG_BUY"


def confidence_for(
    call: str, composite: Decimal, signals: list[Decimal]
) -> tuple[Decimal, str]:
    """FR-28 (§6, A1-ratified): returns (confidence_pct 2dp, level)."""
    if call == "HOLD":
        agree = sum(1 for s in signals if abs(s) <= _HOLD_EDGE)
    else:
        want = 1 if composite > 0 else -1
        # signal == 0 has no sign — it does NOT agree with a directional call.
        agree = sum(1 for s in signals if s != 0 and (1 if s > 0 else -1) == want)
    pct = q2(Decimal(100) * Decimal(agree) / Decimal(len(signals)))
    if pct >= _CONF_HIGH:
        level = "HIGH"
    elif pct >= _CONF_MEDIUM:
        level = "MEDIUM"
    else:
        level = "LOW"
    return pct, level


@dataclass(frozen=True)
class TargetInputs:
    """The day's facts feeding FR-27 (§8)."""

    latest_close: Decimal | None = None
    peer_median_pe: Decimal | None = None
    trailing_eps: Decimal | None = None


def compute_targets(
    inputs: TargetInputs | None, fundamental_available: bool
) -> tuple[Decimal | None, Decimal | None, Decimal | None, bool]:
    """§8: returns (base, bear, bull, target_fundamental_usable), 4dp.
    The usable-guard (A1 completeness patch) applies EVEN IF the fundamental
    scoring module is otherwise ok: EPS ≤ 0 / null peer PE → close-only.
    No latest_close at all → no targets (an honest gap, never a fabricated
    fundamental-only number — "never a silent thinner blend")."""
    if inputs is None:
        return None, None, None, False
    usable = (fundamental_available
              and inputs.trailing_eps is not None and inputs.trailing_eps > 0
              and inputs.peer_median_pe is not None)
    if inputs.latest_close is None:
        return None, None, None, False
    if usable:
        fair_value = inputs.peer_median_pe * inputs.trailing_eps
        base = _q4(_TARGET_W_FUNDAMENTAL * fair_value
                   + _TARGET_W_TECHNICAL * inputs.latest_close)
    else:
        base = _q4(inputs.latest_close)
    return base, _q4(base * _TARGET_BEAR), _q4(base * _TARGET_BULL), usable


@dataclass(frozen=True)
class EngineResult:
    """Everything the recommendation row + golden fixtures need (§1–§9)."""

    composite_signal: Decimal | None       # 2dp; None iff SUPPRESSED
    composite_call: str
    weight_effective: dict[str, Decimal]   # 4dp persisted values (§2)
    data_completeness: Decimal             # |A|/4
    reduced_confidence: bool
    spread: Decimal | None                 # full precision, over survivors
    conflict_flag: bool
    confidence_pct: Decimal | None         # 2dp
    confidence_level: str | None
    target_base: Decimal | None
    target_bear: Decimal | None
    target_bull: Decimal | None
    target_fundamental_usable: bool
    suppressed_reason: str | None
    per_module_breakdown: list[dict[str, Any]]
    missing_modules: tuple[str, ...] = ()


def compute_recommendation(
    signals: dict[str, Decimal | None],
    *,
    weights: dict[str, Decimal] | None = None,
    target_inputs: TargetInputs | None = None,
    notes: dict[str, str | None] | None = None,
    subfields_complete: dict[str, bool] | None = None,
) -> EngineResult:
    """Pure engine core. `signals` maps each of the 4 modules to a
    full-precision Decimal or None (unavailable); modules absent from the
    mapping count as unavailable too (GF-B/GF-C input shape)."""
    weights = weights or DEFAULT_WEIGHTS
    notes = notes or {}
    subfields_complete = subfields_complete or {}
    available = [m for m in MODULES if signals.get(m) is not None]
    missing = tuple(m for m in MODULES if m not in available)
    completeness = Decimal(len(available)) / Decimal(4)

    def breakdown(w_eff: dict[str, Decimal]) -> list[dict[str, Any]]:
        # §9 frozen shape, fixed module order, all 4 always listed (FR-37).
        out = []
        for m in MODULES:
            item: dict[str, Any] = {
                "module": m,
                "signal_score": float(q2(signals[m])) if m in available else None,
                "weight_assigned": float(weights[m]),
                "weight_effective": float(w_eff.get(m, Decimal(0))),
                "status": "ok" if m in available else "unavailable",
                # v1.2.6 §9: intra-module completeness — NOT availability.
                # chip nets-only => ok + subfields_complete:false, no renorm,
                # data_completeness unchanged (GF-CHIP-PARTIAL-intra-module).
                "subfields_complete": subfields_complete.get(m, True),
            }
            if notes.get(m):
                item["subfields_note"] = notes[m]
            out.append(item)
        return out

    if len(missing) >= 2:
        # §4 row 3: SUPPRESS — no score/target/confidence, fixed reason,
        # breakdown still lists all 4 lenses for transparency. No effective
        # weights exist because no composite is computed (all 0.0000).
        return EngineResult(
            composite_signal=None, composite_call="SUPPRESSED",
            weight_effective=dict.fromkeys(MODULES, Decimal("0.0000")),
            data_completeness=completeness,
            reduced_confidence=False,  # suppression supersedes the reduced flag
            spread=None, conflict_flag=False,
            confidence_pct=None, confidence_level=None,
            target_base=None, target_bear=None, target_bull=None,
            target_fundamental_usable=False,
            suppressed_reason=SUPPRESSED_REASON,
            per_module_breakdown=breakdown({}),
            missing_modules=missing,
        )

    # §2: renormalise at FULL precision; round w_eff to 4dp only for storage.
    denom = sum(weights[m] for m in available)
    w_full = {m: weights[m] / denom for m in available}
    w_eff_stored = {m: (_q4(w_full[m]) if m in available else Decimal("0.0000"))
                    for m in MODULES}

    # §3: full-precision composite, rounded ONCE (2dp half-up); §5 band on the
    # rounded value so the persisted score and the call always agree (GF-B/D).
    composite = q2(sum(w_full[m] * signals[m] for m in available))
    call = call_for_composite(composite)

    survivors = [signals[m] for m in available]
    spread = max(survivors) - min(survivors)
    conflict = spread > _CONFLICT_SPREAD  # §7: strictly greater

    confidence_pct, confidence_level = confidence_for(call, composite, survivors)

    base, bear, bull, usable = compute_targets(
        target_inputs, fundamental_available="fundamental" in available
    )
    # §4 + §8: Reduced-Confidence when exactly one lens is down, and ALSO as
    # the framing for a close-only target (unusable fundamental leg) — even if
    # the fundamental scoring module is otherwise ok (FR-27 guard).
    reduced = len(missing) == 1 or (target_inputs is not None and not usable)

    return EngineResult(
        composite_signal=composite, composite_call=call,
        weight_effective=w_eff_stored, data_completeness=completeness,
        reduced_confidence=reduced, spread=spread, conflict_flag=conflict,
        confidence_pct=confidence_pct, confidence_level=confidence_level,
        target_base=base, target_bear=bear, target_bull=bull,
        target_fundamental_usable=usable, suppressed_reason=None,
        per_module_breakdown=breakdown(w_eff_stored), missing_modules=missing,
    )


# Same-day idempotency ONLY: contract §3/§10 + ADR-003 make `recommendation`
# immutable HISTORY — a prior day's row is never touched (the conflict key
# includes rec_date, so DO UPDATE can only ever re-write today's own row on a
# same-run_date re-run). The contract is silent on same-day re-runs, so we
# choose overwrite-same-day (matches every ingestion adapter's upsert idiom).
_UPSERT_RECOMMENDATION = """
    INSERT INTO recommendation (
        ticker_id, rec_date, composite_signal, composite_call,
        target_price_base, target_price_bear, target_price_bull,
        confidence_level, confidence_pct, conflict_flag, horizon_months,
        per_module_breakdown, data_completeness, reduced_confidence,
        methodology_version)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
    ON CONFLICT (ticker_id, rec_date) DO UPDATE SET
        composite_signal = EXCLUDED.composite_signal,
        composite_call = EXCLUDED.composite_call,
        target_price_base = EXCLUDED.target_price_base,
        target_price_bear = EXCLUDED.target_price_bear,
        target_price_bull = EXCLUDED.target_price_bull,
        confidence_level = EXCLUDED.confidence_level,
        confidence_pct = EXCLUDED.confidence_pct,
        conflict_flag = EXCLUDED.conflict_flag,
        horizon_months = EXCLUDED.horizon_months,
        per_module_breakdown = EXCLUDED.per_module_breakdown,
        data_completeness = EXCLUDED.data_completeness,
        reduced_confidence = EXCLUDED.reduced_confidence,
        methodology_version = EXCLUDED.methodology_version
"""


async def persist_recommendation(
    conn: Any,
    *,
    ticker_id: int,
    rec_date: date,
    result: EngineResult,
    horizon_months: int,
    methodology_version: str,
) -> None:
    """Write the daily row (§10 write semantics). Decimal binds for NUMERIC,
    json.dumps for the JSONB breakdown. The row shape satisfies
    ck_rec_suppressed_shape by construction (nulls iff SUPPRESSED)."""
    await conn.execute(
        _UPSERT_RECOMMENDATION,
        ticker_id, rec_date,
        result.composite_signal, result.composite_call,
        result.target_base, result.target_bear, result.target_bull,
        result.confidence_level, result.confidence_pct,
        result.conflict_flag, horizon_months,
        json.dumps(result.per_module_breakdown),
        result.data_completeness.quantize(Decimal("0.001")),
        result.reduced_confidence, methodology_version,
    )


@dataclass
class EngineRunStats:
    tickers_scored: int = 0
    tickers_suppressed: int = 0  # scored=written; suppressed rows counted too
    tickers_skipped: int = 0     # zero modules available -> no row written
    tickers_failed: int = 0
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        msg = (f"tickers scored={self.tickers_scored} "
               f"suppressed={self.tickers_suppressed} "
               f"skipped={self.tickers_skipped} failed={self.tickers_failed}")
        if self.failures:
            msg += f"; failures: {'; '.join(self.failures)}"
        return msg


async def _load_weights(conn: Any) -> tuple[dict[str, Decimal], int]:
    """Live read of user_config.module_weights + horizon (FR-45)."""
    row = await conn.fetchrow(
        "SELECT module_weights, horizon_months FROM user_config WHERE id = 1"
    )
    if row is None:
        return dict(DEFAULT_WEIGHTS), 6
    raw = json.loads(row["module_weights"])
    weights = {m: Decimal(str(raw.get(m, DEFAULT_WEIGHTS[m]))) for m in MODULES}
    total = sum(weights.values())
    if abs(total - Decimal(1)) > Decimal("0.001"):  # §2: must sum to 1.0 ±0.001
        logger.warning("module_weights sum %s outside 1.0±0.001 — using defaults", total)
        weights = dict(DEFAULT_WEIGHTS)
    return weights, int(row["horizon_months"])


async def score_ticker(
    conn: Any,
    ticker: Any,
    rec_date: date,
    weights: dict[str, Decimal],
    horizon_months: int,
    methodology_version: str,
    news_fetch_override: str | None = None,
) -> EngineResult | None:
    """Compute all 4 lens signals for one ticker and persist the daily
    recommendation. Returns None (no row) when NO module has data at all —
    a fresh install shouldn't mint all-unavailable suppressed rows.
    news_fetch_override (task #20, ADR-009) threads an on-demand run's
    in-memory gdelt fetch outcome to the news lens (the §4a seam); the daily
    engine passes None and news_signal reads the pipeline_run row as before."""
    technical, latest_close = await technical_signal(conn, ticker["id"], rec_date)
    fundamental, peer_median_pe, trailing_eps = await fundamental_signal(conn, ticker["id"])
    chip = await chip_signal(conn, ticker["id"], ticker["exchange"])
    news = await news_signal(conn, ticker["id"], rec_date, ticker["full_symbol"],
                             news_fetch_override=news_fetch_override)

    lenses: dict[str, ModuleSignal] = {
        "technical": technical, "fundamental": fundamental,
        "chip": chip, "news": news,
    }
    if all(not s.available for s in lenses.values()) and latest_close is None:
        return None

    result = compute_recommendation(
        {m: s.signal for m, s in lenses.items()},
        weights=weights,
        target_inputs=TargetInputs(latest_close=latest_close,
                                   peer_median_pe=peer_median_pe,
                                   trailing_eps=trailing_eps),
        notes={m: s.note for m, s in lenses.items()},
        subfields_complete={m: s.subfields_complete for m, s in lenses.items()},
    )
    await persist_recommendation(
        conn, ticker_id=ticker["id"], rec_date=rec_date, result=result,
        horizon_months=horizon_months, methodology_version=methodology_version,
    )
    return result


async def run_engine(conn: Any, rec_date: date) -> EngineRunStats:
    """Pipeline stage: signals + recommendation for every covered ticker with
    at least minimum data; per-ticker isolation (§22.4 spirit)."""
    stats = EngineRunStats()
    weights, horizon_months = await _load_weights(conn)
    methodology_version = get_settings().methodology_version

    tickers = await conn.fetch(
        "SELECT id, full_symbol, exchange FROM ticker WHERE is_covered ORDER BY id"
    )
    for t in tickers:
        try:
            result = await score_ticker(conn, t, rec_date, weights,
                                        horizon_months, methodology_version)
        except Exception as exc:  # per-ticker isolation
            stats.tickers_failed += 1
            stats.failures.append(f"{t['full_symbol']}: {exc}")
            logger.warning("engine failed for %s: %s", t["full_symbol"], exc)
            continue
        if result is None:
            stats.tickers_skipped += 1
        elif result.composite_call == "SUPPRESSED":
            stats.tickers_suppressed += 1
            stats.tickers_scored += 1
        else:
            stats.tickers_scored += 1
    return stats
