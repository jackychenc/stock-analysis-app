"""News / informational lens (FR-15): GDELT headlines + VADER sentiment.

Rubric: mean VADER compound over the lookback window, scaled from [-1, +1]
onto the contract §1 signal range [-2, +2] (x2), clamped.

Missing-news semantics — UNAVAILABLE, not neutral: FR-15's "default to
neutral" is superseded by domain-contract §4's missing-data table and the
golden fixtures (GF-B/GF-C treat a missing module as unavailable, feeding
renormalisation/suppression). A fabricated neutral 0 would silently dilute
the composite — exactly the silent-degradation failure mode the A6 D2 BLOCK
gate forbids. GDELT ingestion is task #12; until it lands, news_item has no
rows and this lens reads honestly unavailable.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.batch.signals import ModuleSignal, clamp_signal, unavailable

WINDOW_DAYS = 7  # lookback window for "the day's" news context
SENTIMENT_SCALE = Decimal(2)  # VADER [-1,1] -> signal [-2,2]


def news_score(sentiments: list[Decimal]) -> ModuleSignal:
    """Pure core over the window's VADER compound values."""
    if not sentiments:
        return unavailable("no news items in window")
    mean = sum(sentiments) / Decimal(len(sentiments))
    return ModuleSignal(
        signal=clamp_signal(mean * SENTIMENT_SCALE), status="ok",
        note=f"{len(sentiments)} headlines over {WINDOW_DAYS}d",
    )


async def news_signal(conn: Any, ticker_id: int, asof: date) -> ModuleSignal:
    window_start = datetime.combine(asof - timedelta(days=WINDOW_DAYS), time.min, tzinfo=UTC)
    rows = await conn.fetch(
        "SELECT sentiment FROM news_item"
        " WHERE ticker_id = $1 AND sentiment IS NOT NULL AND published_at >= $2",
        ticker_id, window_start,
    )
    return news_score([r["sentiment"] for r in rows])
