"""Technical lens (FR-12): MA20 / MA60 / RSI14 / MACD(12,26,9) from price_bar
closes, pure Decimal functions (no pandas), plus the derived [-2,+2] signal.

Indicator conventions (documented — the contract names the indicators but not
their arithmetic):
- MA: simple moving average of the last N closes.
- RSI14: Wilder's smoothing — first average = mean of the first 14 changes,
  then avg = (prev*13 + change)/14. All-gain window => 100; all-loss => 0.
- MACD: EMA12 − EMA26, signal = EMA9 of the MACD line, hist = macd − signal.
  Each EMA is seeded with the SMA of its first `period` values (the standard
  TA seeding), k = 2/(period+1).

Scoring rubric (A5 choice; contract §5 fixes only the composite bands — see
module docstring of app.batch.signals). Three documented component votes,
summed and clamped to [-2, +2]:
- trend  (±1.0): close > MA20 > MA60 → +1.0; close < MA20 < MA60 → −1.0;
                 otherwise close vs MA20 alone → ±0.5 (0 when equal).
- RSI    (±0.5): contrarian — RSI < 30 (oversold) → +0.5; RSI > 70
                 (overbought) → −0.5; else 0.
- MACD   (±0.5): histogram > 0 → +0.5; < 0 → −0.5; 0 → 0.

Persistence: indicators + score upsert into technical_indicator on
(ticker_id, calc_date). That table carries no ingested_at column (checked
against db/schema.sql) — provenance rides the recommendation row instead.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.batch.signals import ModuleSignal, clamp_signal, q2, unavailable

# Data floor: MA60 needs 60 closes (also covers MACD's 26+9−1=34 and RSI's 15).
MIN_BARS = 60
_HISTORY_BARS = 150  # fetch window: enough for smoothing warm-up, bounded read

TREND_VOTE = Decimal("1.0")
TREND_HALF_VOTE = Decimal("0.5")
RSI_VOTE = Decimal("0.5")
RSI_OVERSOLD = Decimal("30")
RSI_OVERBOUGHT = Decimal("70")
MACD_VOTE = Decimal("0.5")


def sma(values: list[Decimal], n: int) -> Decimal | None:
    """Simple moving average of the LAST n values (full precision)."""
    if len(values) < n:
        return None
    window = values[-n:]
    return sum(window) / Decimal(n)


def ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    """EMA over `values`, seeded with the SMA of the first `period` values.
    Returns one EMA point per input from index period-1 onward."""
    if len(values) < period:
        return []
    k = Decimal(2) / Decimal(period + 1)
    ema = sum(values[:period]) / Decimal(period)
    out = [ema]
    for v in values[period:]:
        ema = ema + (v - ema) * k
        out.append(ema)
    return out


def rsi14(closes: list[Decimal], period: int = 14) -> Decimal | None:
    """Wilder RSI over the full provided series (deterministic per FR-19)."""
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, Decimal(0)) for c in changes]
    losses = [max(-c, Decimal(0)) for c in changes]
    avg_gain = sum(gains[:period]) / Decimal(period)
    avg_loss = sum(losses[:period]) / Decimal(period)
    for g, lo in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + g) / Decimal(period)
        avg_loss = (avg_loss * (period - 1) + lo) / Decimal(period)
    if avg_loss == 0:
        return Decimal(100)
    rs = avg_gain / avg_loss
    return Decimal(100) - Decimal(100) / (1 + rs)


def macd_12_26_9(
    closes: list[Decimal],
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Returns (macd, macd_signal, macd_hist) for the latest close, or None
    when the series is too short (needs 26+9−1 = 34 closes)."""
    fast, slow, signal_period = 12, 26, 9
    if len(closes) < slow + signal_period - 1:
        return None
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    # Align: EMA26 starts at index 25; EMA12 at index 11 -> drop its first 14.
    macd_line = [f - s for f, s in zip(ema_fast[slow - fast:], ema_slow, strict=True)]
    signal_line = ema_series(macd_line, signal_period)
    macd = macd_line[-1]
    sig = signal_line[-1]
    return macd, sig, macd - sig


def technical_score(
    close: Decimal,
    ma20: Decimal,
    ma60: Decimal,
    rsi: Decimal,
    macd_hist: Decimal,
) -> Decimal:
    """Documented rubric (see module docstring): trend ±1.0, RSI ±0.5,
    MACD-hist ±0.5, summed, clamped to [-2, +2]. Full precision."""
    if close > ma20 > ma60:
        trend = TREND_VOTE
    elif close < ma20 < ma60:
        trend = -TREND_VOTE
    elif close > ma20:
        trend = TREND_HALF_VOTE
    elif close < ma20:
        trend = -TREND_HALF_VOTE
    else:
        trend = Decimal(0)

    if rsi < RSI_OVERSOLD:
        momentum = RSI_VOTE
    elif rsi > RSI_OVERBOUGHT:
        momentum = -RSI_VOTE
    else:
        momentum = Decimal(0)

    if macd_hist > 0:
        macd_vote = MACD_VOTE
    elif macd_hist < 0:
        macd_vote = -MACD_VOTE
    else:
        macd_vote = Decimal(0)

    return clamp_signal(trend + momentum + macd_vote)


def compute_technical(
    closes: list[Decimal],
) -> tuple[ModuleSignal, dict[str, Decimal] | None]:
    """Pure core: (signal, indicators) from an ascending-date close series.
    Fewer than MIN_BARS closes -> honest unavailable (feeds contract §4)."""
    if len(closes) < MIN_BARS:
        return unavailable(f"insufficient price history ({len(closes)}/{MIN_BARS} bars)"), None
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    rsi = rsi14(closes)
    macd_tuple = macd_12_26_9(closes)
    assert ma20 is not None and ma60 is not None and rsi is not None and macd_tuple is not None
    macd, macd_sig, macd_hist = macd_tuple
    score = technical_score(closes[-1], ma20, ma60, rsi, macd_hist)
    indicators = {
        "ma20": ma20, "ma60": ma60, "rsi14": rsi,
        "macd": macd, "macd_signal": macd_sig, "macd_hist": macd_hist,
    }
    return ModuleSignal(signal=score, status="ok"), indicators


_UPSERT_INDICATOR = """
    INSERT INTO technical_indicator (ticker_id, calc_date, ma20, ma60, rsi14,
                                     macd, macd_signal, macd_hist, score)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (ticker_id, calc_date) DO UPDATE SET
        ma20 = EXCLUDED.ma20, ma60 = EXCLUDED.ma60, rsi14 = EXCLUDED.rsi14,
        macd = EXCLUDED.macd, macd_signal = EXCLUDED.macd_signal,
        macd_hist = EXCLUDED.macd_hist, score = EXCLUDED.score
"""


def _q(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


async def technical_signal(
    conn: Any, ticker_id: int, calc_date: date
) -> tuple[ModuleSignal, Decimal | None]:
    """Fetch closes, compute + persist indicators, return (signal, latest_close).
    latest_close feeds the FR-27 target's technical reference (contract §8) —
    it is returned even when the scoring window is too short (an honest close
    is still a close)."""
    rows = await conn.fetch(
        "SELECT bar_date, close FROM price_bar"
        " WHERE ticker_id = $1 AND close IS NOT NULL"
        " ORDER BY bar_date DESC LIMIT $2",
        ticker_id, _HISTORY_BARS,
    )
    closes = [r["close"] for r in reversed(rows)]  # ascending date order
    latest_close = closes[-1] if closes else None
    signal, indicators = compute_technical(closes)
    if indicators is not None and signal.signal is not None:
        # Persistence rounding only here (contract §2): NUMERIC(18,4) MAs,
        # NUMERIC(6,2) RSI, NUMERIC(18,6) MACD family, NUMERIC(4,2) score.
        await conn.execute(
            _UPSERT_INDICATOR, ticker_id, calc_date,
            _q(indicators["ma20"], "0.0001"), _q(indicators["ma60"], "0.0001"),
            _q(indicators["rsi14"], "0.01"), _q(indicators["macd"], "0.000001"),
            _q(indicators["macd_signal"], "0.000001"),
            _q(indicators["macd_hist"], "0.000001"), q2(signal.signal),
        )
    return signal, latest_close
