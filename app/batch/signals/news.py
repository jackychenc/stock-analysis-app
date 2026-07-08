"""News / informational lens (FR-15): GDELT headlines + VADER sentiment.

Rubric: mean VADER compound over the lookback window, scaled from [-1, +1]
onto the contract §1 signal range [-2, +2] (x2), clamped.

Missing-news semantics — the pre-#12 "missing news => unavailable" paragraph
is SUPERSEDED by contract v1.2.8 §4a (+ A1/A6/A8 rulings). The lens is now a
TERNARY, and the discriminator is the FETCH OUTCOME at the adapter boundary
(the gdelt pipeline_run row), NEVER the news_item row count — error and empty
both leave 0 rows, so row count cannot tell them apart (A8 integrity rule):
- fetch/source failure (no run, non-ok status, or this ticker named in the
  run message's failed_tickers= token) -> unavailable, signal None — the
  engine renormalises (data_completeness 0.75);
- fetch succeeded + 0 headlines in window -> ok, signal 0.00, note
  "0 headlines in window" — "no news is neutral news": a genuinely quiet
  week is a real observation, full completeness, NO renormalisation;
- fetch succeeded + N headlines -> ok, mean(compound) x2, clamped.

MACHINE-STABLE COUPLING: the `failed_tickers=SYM1,SYM2` token (comma-joined,
no spaces) is emitted by adapters/gdelt_adapter.NewsIngestStats.summary()
into pipeline_run.message and parsed here. Change the format only in
lockstep with that module.
"""

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.batch.signals import ModuleSignal, clamp_signal, unavailable

WINDOW_DAYS = 7  # lookback window for "the day's" news context
SENTIMENT_SCALE = Decimal(2)  # VADER [-1,1] -> signal [-2,2]

# The exact wording is load-bearing for QA (contract v1.2.8 §4a): the neutral
# empty-window note must be distinguishable from an unavailable fetch.
EMPTY_WINDOW_NOTE = "0 headlines in window"

_FAILED_TICKERS_RE = re.compile(r"failed_tickers=(\S+)")


def parse_failed_tickers(message: str | None) -> frozenset[str]:
    """Extract the gdelt run message's failed_tickers= token (see docstring)."""
    match = _FAILED_TICKERS_RE.search(message or "")
    if not match:
        return frozenset()
    return frozenset(s for s in match.group(1).split(",") if s)


def news_score(sentiments: list[Decimal]) -> ModuleSignal:
    """Pure core over the window's VADER compound values. Callers must have
    already established that the fetch SUCCEEDED — an empty list here means
    a quiet news week (neutral 0.00), not a missing module (§4a)."""
    if not sentiments:
        return ModuleSignal(signal=Decimal("0.00"), status="ok",
                            note=EMPTY_WINDOW_NOTE)
    mean = sum(sentiments) / Decimal(len(sentiments))
    return ModuleSignal(
        signal=clamp_signal(mean * SENTIMENT_SCALE), status="ok",
        note=f"{len(sentiments)} headlines over {WINDOW_DAYS}d",
    )


async def news_signal(conn: Any, ticker_id: int, asof: date,
                      full_symbol: str) -> ModuleSignal:
    # A8 integrity rule: status derives from the fetch outcome FIRST; the
    # window rows are only read once the fetch is known good for this ticker.
    run = await conn.fetchrow(
        "SELECT status, message FROM pipeline_run"
        " WHERE run_date = $1 AND source_name = 'gdelt'",
        asof,
    )
    if run is None:
        return unavailable("gdelt not fetched for this date")
    if run["status"] != "ok":
        return unavailable(f"gdelt source {run['status']}")
    if full_symbol in parse_failed_tickers(run["message"]):
        return unavailable("gdelt ticker query failed")

    # Bounded window [asof-7d 00:00, asof+1d 00:00) UTC — the upper bound
    # keeps historical re-reads deterministic (later news never leaks back).
    window_start = datetime.combine(asof - timedelta(days=WINDOW_DAYS),
                                    time.min, tzinfo=UTC)
    window_end = datetime.combine(asof + timedelta(days=1), time.min, tzinfo=UTC)
    rows = await conn.fetch(
        "SELECT sentiment FROM news_item"
        " WHERE ticker_id = $1 AND sentiment IS NOT NULL"
        " AND published_at >= $2 AND published_at < $3",
        ticker_id, window_start, window_end,
    )
    return news_score([r["sentiment"] for r in rows])
