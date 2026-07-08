"""Benchmark-relative backtest engine (task #13) — domain-contract v1.2.8 §10.

Binding contract rules (A6 D5 goldens, BLOCK-gate):
- Correct BUY/STRONG_BUY iff stock_return − benchmark_return > +2.0pp
  (STRICT: exactly +2.0pp is NOT correct — GF-BT-buy-edge); correct
  SELL/STRONG_SELL iff the delta < −2.0pp. HOLD and SUPPRESSED are EXCLUDED
  from accuracy entirely (counted neither correct nor incorrect).
- Benchmark by market: full_symbol ending .TW/.TWO → ^TWII, otherwise ^GSPC.
- The 'full' segment aggregates recs whose data_completeness == 1.000 ONLY;
  partial days (dc < 1.0) live in the 'partial' segment — NEVER blended.
- History gate: < 12 months between the earliest recommendation row (ANY
  ticker) and as_of ⇒ insufficient_history=true and null accuracies (no
  misleading number); sample_size may still be reported.
- All money math in Decimal; accuracies/returns quantised 4dp at the edge
  (NUMERIC(6,4) / NUMERIC(8,4)).
- methodology_version stamped on every backtest_result row (ADR-003).

Engine-owned conventions (A5 choices riding methodology_version — the D5
goldens deliberately do not pin these):
- Return over horizon: for a rec on date D with horizon H months, entry =
  close of the latest bar ≤ D and exit = close of the latest bar ≤ D+H
  months; a rec is EVALUABLE only when D+H ≤ as_of AND both the stock and
  its benchmark have such bars (no peeking, no partial-horizon
  extrapolation). The benchmark return uses the same convention.
- Rolling window: a backtest at as_of with window_months W includes the
  evaluable recs with rec_date in [as_of − W months, as_of].
- estimated_return = mean of sign·delta_pp over the evaluated (non-HOLD,
  non-SUPPRESSED) recs, sign = +1 for BUY/STRONG_BUY and −1 for
  SELL/STRONG_SELL — the average benchmark-relative excess a follower of
  the calls would have captured; null when no samples or insufficient
  history. Table rows keep it per segment; the ticker-scoped endpoint
  reports one value over both segments (the never-blend rule pins the
  ACCURACY headline — the segment split stays visible in the table rows).
- Month arithmetic is dateutil-free: add_months clamps the day-of-month
  (Jan 31 + 1mo → Feb 28/29).
"""

import calendar
import logging
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.config import get_settings
from app.schemas.contracts import BacktestResult

logger = logging.getLogger(__name__)

WINDOWS = (3, 6, 12)                    # window_months values persisted per run
SEGMENTS = ("full", "partial")          # completeness_segment values (schema)
THRESHOLD_PP = Decimal("2.0")           # §10: strict > +2.0pp / < −2.0pp
MIN_HISTORY_MONTHS = 12                 # §10 history gate
TW_BENCHMARK = "^TWII"
US_BENCHMARK = "^GSPC"
_BUY_CALLS = frozenset({"BUY", "STRONG_BUY"})
_SELL_CALLS = frozenset({"SELL", "STRONG_SELL"})
_EXCLUDED_CALLS = frozenset({"HOLD", "SUPPRESSED"})  # never in accuracy (§10)


def _q4(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# --- pure core (unit-testable without DB) ------------------------------------

def benchmark_for(full_symbol: str) -> str:
    """§10: benchmark matched to the ticker's market — TW listings (.TW TWSE,
    .TWO TPEx) track ^TWII; everything else in MVP scope is US → ^GSPC."""
    if full_symbol.endswith((".TW", ".TWO")):
        return TW_BENCHMARK
    return US_BENCHMARK


def delta_pp(stock_ret_pct: Decimal, bench_ret_pct: Decimal) -> Decimal:
    """Benchmark-relative excess in percentage points (full precision)."""
    return stock_ret_pct - bench_ret_pct


def evaluate_call(
    call: str, stock_ret_pct: Decimal, bench_ret_pct: Decimal
) -> bool | None:
    """§10 correctness. None = EXCLUDED from accuracy (HOLD/SUPPRESSED).
    Strict inequalities: exactly ±2.0pp is NOT correct (GF-BT-buy-edge)."""
    if call in _EXCLUDED_CALLS:
        return None
    delta = delta_pp(stock_ret_pct, bench_ret_pct)
    if call in _BUY_CALLS:
        return delta > THRESHOLD_PP
    if call in _SELL_CALLS:
        return delta < -THRESHOLD_PP
    raise ValueError(f"unknown composite_call: {call!r}")


def add_months(d: date, months: int) -> date:
    """Dateutil-free month arithmetic; day-of-month clamped to the target
    month's length (Jan 31 + 1mo → Feb 28/29)."""
    year, month0 = divmod(d.year * 12 + (d.month - 1) + months, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def segment_for(data_completeness: Decimal | None) -> str:
    """'full' iff the rec's data_completeness == 1.000; anything less (or
    unknown) is 'partial' — the two are aggregated separately, never blended."""
    if data_completeness is None:
        return "partial"
    return "full" if Decimal(str(data_completeness)) == 1 else "partial"


def history_insufficient(earliest_rec_date: date | None, as_of: date) -> bool:
    """§10 gate: < 12 months between the earliest recommendation row (any
    ticker) and as_of — or no history at all — means no headline number."""
    if earliest_rec_date is None:
        return True
    return add_months(earliest_rec_date, MIN_HISTORY_MONTHS) > as_of


@dataclass(frozen=True)
class EvaluatedRec:
    """One matured, priced, non-excluded rec: the unit of accuracy math."""

    rec_date: date
    segment: str               # 'full' | 'partial' (segment_for)
    correct: bool              # §10 strict ±2.0pp rule
    signed_delta_pp: Decimal   # sign(call)·delta_pp, full precision


@dataclass(frozen=True)
class SegmentSummary:
    """One backtest_result row's payload (window × segment)."""

    rolling_accuracy: Decimal | None   # 4dp fraction; None if gated/empty
    estimated_return: Decimal | None   # 4dp mean signed delta; None likewise
    sample_size: int                   # reported even under the history gate


def summarize_segment(
    evaluated: list[EvaluatedRec], insufficient: bool
) -> SegmentSummary:
    """Accuracy + estimated return over one segment's evaluated recs. The
    history gate nulls the numbers but the sample size stays honest."""
    n = len(evaluated)
    if insufficient or n == 0:
        return SegmentSummary(None, None, n)
    correct = sum(1 for e in evaluated if e.correct)
    accuracy = _q4(Decimal(correct) / Decimal(n))
    estimated = _q4(sum(e.signed_delta_pp for e in evaluated) / Decimal(n))
    return SegmentSummary(accuracy, estimated, n)


# --- DB-facing layer ----------------------------------------------------------

@dataclass
class BacktestRunStats:
    recs_seen: int = 0        # recs in the widest (12mo) window
    recs_evaluated: int = 0   # matured, priced, non-excluded
    recs_excluded: int = 0    # HOLD/SUPPRESSED — never in accuracy
    recs_immature: int = 0    # horizon end beyond as_of — no peeking
    recs_unpriced: int = 0    # stock/benchmark bars missing at entry/exit
    rows_upserted: int = 0
    insufficient_history: bool = True
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        msg = (f"recs seen={self.recs_seen} evaluated={self.recs_evaluated} "
               f"excluded={self.recs_excluded} immature={self.recs_immature} "
               f"unpriced={self.recs_unpriced}; "
               f"rows upserted={self.rows_upserted}; "
               f"insufficient_history={str(self.insufficient_history).lower()}")
        if self.failures:
            msg += f"; failures: {'; '.join(self.failures)}"
        return msg


_SELECT_RECS = """
    SELECT r.ticker_id, r.rec_date, r.composite_call, r.horizon_months,
           r.data_completeness, t.full_symbol
    FROM recommendation r
    JOIN ticker t ON t.id = r.ticker_id
    WHERE r.rec_date >= $1 AND r.rec_date <= $2
    ORDER BY r.rec_date
"""

_SELECT_RECS_FOR_TICKER = """
    SELECT r.ticker_id, r.rec_date, r.composite_call, r.horizon_months,
           r.data_completeness, t.full_symbol
    FROM recommendation r
    JOIN ticker t ON t.id = r.ticker_id
    WHERE r.rec_date >= $1 AND r.rec_date <= $2 AND r.ticker_id = $3
    ORDER BY r.rec_date
"""

_SELECT_TICKER_IDS = "SELECT id, full_symbol FROM ticker ORDER BY id"

_SELECT_BARS = """
    SELECT ticker_id, bar_date, close FROM price_bar
    WHERE bar_date <= $1
    ORDER BY ticker_id, bar_date
"""

# Same-day recompute allowed; history immutable otherwise — the conflict key
# includes as_of_date, so a re-run only ever rewrites its own day's rows.
_UPSERT_BACKTEST = """
    INSERT INTO backtest_result (as_of_date, window_months, completeness_segment,
                                 rolling_accuracy, estimated_return, sample_size,
                                 methodology_version)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (as_of_date, window_months, completeness_segment, methodology_version)
    DO UPDATE SET
        rolling_accuracy = EXCLUDED.rolling_accuracy,
        estimated_return = EXCLUDED.estimated_return,
        sample_size = EXCLUDED.sample_size
"""


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _close_on_or_before(
    bars: tuple[list[date], list[Decimal]] | None, d: date
) -> Decimal | None:
    """Close of the latest bar ≤ d (no peeking past d)."""
    if bars is None:
        return None
    dates, closes = bars
    i = bisect_right(dates, d)
    return closes[i - 1] if i else None


async def _load_bars(
    conn: Any, as_of: date
) -> dict[int, tuple[list[date], list[Decimal]]]:
    """price_bar closes ≤ as_of, grouped per ticker as parallel sorted lists
    (bisect-friendly). NULL closes are skipped — no fabricated prices."""
    rows = await conn.fetch(_SELECT_BARS, as_of)
    grouped: dict[int, list[tuple[date, Decimal]]] = {}
    for r in rows:
        if r["close"] is None:
            continue
        grouped.setdefault(r["ticker_id"], []).append((r["bar_date"], _dec(r["close"])))
    out: dict[int, tuple[list[date], list[Decimal]]] = {}
    for ticker_id, pairs in grouped.items():
        pairs.sort()  # bisect needs strict date order — never trust row order
        out[ticker_id] = ([d for d, _ in pairs], [c for _, c in pairs])
    return out


def _evaluate_recs(
    recs: list[Any],
    bars: dict[int, tuple[list[date], list[Decimal]]],
    symbol_to_id: dict[str, int],
    as_of: date,
    stats: BacktestRunStats,
) -> list[EvaluatedRec]:
    """Apply the maturity + pricing conventions (module docstring) and the §10
    correctness rule to each rec; counters stay honest about every drop."""
    evaluated: list[EvaluatedRec] = []
    for rec in recs:
        stats.recs_seen += 1
        call = rec["composite_call"]
        if call in _EXCLUDED_CALLS:
            stats.recs_excluded += 1
            continue
        exit_date = add_months(rec["rec_date"], int(rec["horizon_months"]))
        if exit_date > as_of:
            stats.recs_immature += 1  # partial horizon is never extrapolated
            continue
        bench_id = symbol_to_id.get(benchmark_for(rec["full_symbol"]))
        stock_bars = bars.get(rec["ticker_id"])
        bench_bars = bars.get(bench_id) if bench_id is not None else None
        entry = _close_on_or_before(stock_bars, rec["rec_date"])
        exit_ = _close_on_or_before(stock_bars, exit_date)
        b_entry = _close_on_or_before(bench_bars, rec["rec_date"])
        b_exit = _close_on_or_before(bench_bars, exit_date)
        if (entry is None or exit_ is None or b_entry is None or b_exit is None
                or entry <= 0 or b_entry <= 0):
            stats.recs_unpriced += 1  # missing coverage: dropped, counted
            continue
        stock_ret = (exit_ - entry) / entry * 100
        bench_ret = (b_exit - b_entry) / b_entry * 100
        correct = evaluate_call(call, stock_ret, bench_ret)
        sign = Decimal(1) if call in _BUY_CALLS else Decimal(-1)
        evaluated.append(EvaluatedRec(
            rec_date=rec["rec_date"],
            segment=segment_for(rec["data_completeness"]),
            correct=correct,
            signed_delta_pp=sign * delta_pp(stock_ret, bench_ret),
        ))
        stats.recs_evaluated += 1
    return evaluated


def _window_segment(
    evaluated: list[EvaluatedRec], start: date, segment: str
) -> list[EvaluatedRec]:
    return [e for e in evaluated if e.rec_date >= start and e.segment == segment]


async def run_backtest(conn: Any, as_of: date) -> BacktestRunStats:
    """Pipeline stage: evaluate matured recs against their market benchmark
    and UPSERT the GLOBAL (3, 6, 12) × ('full', 'partial') backtest_result
    rows for as_of. No recommendation history at all → nothing written (the
    pipeline reports the stage 'unavailable')."""
    stats = BacktestRunStats()
    methodology_version = get_settings().methodology_version

    earliest = await conn.fetchval("SELECT min(rec_date) FROM recommendation")
    if earliest is None:
        return stats  # no history yet — an honest no-op, never fabricated rows
    stats.insufficient_history = history_insufficient(earliest, as_of)

    recs = await conn.fetch(_SELECT_RECS, add_months(as_of, -max(WINDOWS)), as_of)
    ticker_rows = await conn.fetch(_SELECT_TICKER_IDS)
    symbol_to_id = {r["full_symbol"]: r["id"] for r in ticker_rows}
    bars = await _load_bars(conn, as_of)

    evaluated = _evaluate_recs(recs, bars, symbol_to_id, as_of, stats)

    for window in WINDOWS:
        start = add_months(as_of, -window)
        for segment in SEGMENTS:
            summary = summarize_segment(
                _window_segment(evaluated, start, segment),
                stats.insufficient_history,
            )
            await conn.execute(
                _UPSERT_BACKTEST, as_of, window, segment,
                summary.rolling_accuracy, summary.estimated_return,
                summary.sample_size, methodology_version,
            )
            stats.rows_upserted += 1
    return stats


async def compute_ticker_backtest(
    conn: Any,
    ticker: Any,
    *,
    window_months: int = 6,
    as_of: date | None = None,
) -> BacktestResult:
    """TICKER-SCOPED backtest computed at request time from the same pure
    core, filtered to this ticker's recs. The A3 scope ruling on serving the
    GLOBAL backtest_result rows instead is pending — the choice is isolated
    here so the route swaps implementations with a one-function change.

    The <12mo gate stays GLOBAL (§10: earliest recommendation row, any
    ticker) — a freshly covered ticker doesn't reset the honesty clock."""
    as_of = as_of or date.today()
    settings = get_settings()
    benchmark = benchmark_for(ticker["full_symbol"])

    def result(full: SegmentSummary | None, partial: SegmentSummary | None,
               estimated: Decimal | None, insufficient: bool) -> BacktestResult:
        return BacktestResult(
            window_months=window_months,
            rolling_accuracy_full=(
                None if full is None or full.rolling_accuracy is None
                else float(full.rolling_accuracy)),
            rolling_accuracy_partial=(
                None if partial is None or partial.rolling_accuracy is None
                else float(partial.rolling_accuracy)),
            estimated_return=None if estimated is None else float(estimated),
            benchmark=benchmark,
            insufficient_history=insufficient,
            methodology_version=settings.methodology_version,
            disclaimer=settings.disclaimer_text,  # FR-39: config-sourced
            disclaimer_version=settings.disclaimer_version,
        )

    earliest = await conn.fetchval("SELECT min(rec_date) FROM recommendation")
    insufficient = history_insufficient(earliest, as_of)
    if earliest is None:
        return result(None, None, None, insufficient)

    start = add_months(as_of, -window_months)
    recs = await conn.fetch(_SELECT_RECS_FOR_TICKER, start, as_of, ticker["id"])
    if not recs:
        return result(None, None, None, insufficient)
    ticker_rows = await conn.fetch(_SELECT_TICKER_IDS)
    symbol_to_id = {r["full_symbol"]: r["id"] for r in ticker_rows}
    bars = await _load_bars(conn, as_of)

    stats = BacktestRunStats()  # counters only; nothing persisted on this path
    evaluated = _evaluate_recs(recs, bars, symbol_to_id, as_of, stats)

    full = summarize_segment(_window_segment(evaluated, start, "full"), insufficient)
    partial = summarize_segment(_window_segment(evaluated, start, "partial"), insufficient)
    # Endpoint estimated_return spans both segments (module docstring: the
    # never-blend rule pins accuracies; segment rows stay segmented).
    estimated = None
    if evaluated and not insufficient:
        estimated = _q4(sum(e.signed_delta_pp for e in evaluated)
                        / Decimal(len(evaluated)))
    return result(full, partial, estimated, insufficient)
