"""Task #10 signal-calculator unit tests — indicator arithmetic on small
hand-computed series (FR-12/FR-19), chip aux-partial visibility (Cindy binding
condition / A6 D2), fundamental peer comparison (FR-13), and the news lens's
unavailable-not-neutral rule (contract §4 over FR-15's neutral wording)."""

from datetime import date
from decimal import Decimal

from app.batch.signals import ModuleSignal, clamp_signal, sign_of
from app.batch.signals.chip import US_LABEL, chip_score_tw, chip_score_us
from app.batch.signals.fundamental import fundamental_score, median
from app.batch.signals.news import news_score
from app.batch.signals.technical import (
    MIN_BARS,
    compute_technical,
    ema_series,
    macd_12_26_9,
    rsi14,
    sma,
    technical_score,
    technical_signal,
)


def dseq(*values) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


# --- MA / EMA -------------------------------------------------------------------

def test_sma_last_n_values():
    assert sma(dseq(1, 2, 3, 4, 5), 3) == Decimal("4")  # (3+4+5)/3
    assert sma(dseq(1, 2), 3) is None  # too short -> honest None


def test_ema_seeded_with_sma_then_smoothed():
    # period 3: seed = SMA(1,2,3) = 2; k = 2/(3+1) = 0.5; next = 2+(4-2)/2 = 3
    assert ema_series(dseq(1, 2, 3, 4), 3) == [Decimal("2"), Decimal("3")]
    assert ema_series(dseq(1, 2), 3) == []


def test_ema_of_constant_series_is_constant():
    assert set(ema_series([Decimal("7")] * 40, 12)) == {Decimal("7")}


# --- RSI14 (Wilder) --------------------------------------------------------------

def test_rsi_all_gains_is_100():
    closes = dseq(*range(100, 116))  # 15 closes, 14 straight gains
    assert rsi14(closes) == Decimal("100")


def test_rsi_all_losses_is_0():
    closes = dseq(*range(115, 99, -1))
    assert rsi14(closes) == Decimal("0")


def test_rsi_balanced_gains_losses_is_50():
    # +1/−1 alternating: 7 gains of 1, 7 losses of 1 -> RS=1 -> RSI=50
    closes = [Decimal(100)]
    for i in range(14):
        closes.append(closes[-1] + (1 if i % 2 == 0 else -1))
    assert rsi14(closes) == Decimal("50")


def test_rsi_too_short_is_none():
    assert rsi14(dseq(*range(10))) is None


# --- MACD(12,26,9) ---------------------------------------------------------------

def test_macd_constant_series_is_flat_zero():
    macd, sig, hist = macd_12_26_9([Decimal("50")] * 60)
    assert (macd, sig, hist) == (Decimal("0"), Decimal("0"), Decimal("0"))


def test_macd_uptrend_is_positive():
    macd, _sig, _hist = macd_12_26_9([Decimal(100 + i) for i in range(60)])
    assert macd > 0  # fast EMA rides above slow EMA in a steady uptrend


def test_macd_too_short_is_none():
    assert macd_12_26_9([Decimal(1)] * 33) is None  # needs 26+9-1 = 34


# --- technical rubric -------------------------------------------------------------

def test_technical_score_full_bull_alignment():
    # close > MA20 > MA60 (+1), RSI mid (0), hist > 0 (+0.5) -> +1.5
    score = technical_score(Decimal("110"), Decimal("105"), Decimal("100"),
                            Decimal("55"), Decimal("0.3"))
    assert score == Decimal("1.5")


def test_technical_score_full_bear_with_overbought():
    # close < MA20 < MA60 (−1), RSI > 70 (−0.5), hist < 0 (−0.5) -> −2 (clamped)
    score = technical_score(Decimal("90"), Decimal("95"), Decimal("100"),
                            Decimal("75"), Decimal("-0.3"))
    assert score == Decimal("-2")


def test_technical_score_oversold_contrarian_vote():
    # close > MA20 but MA20 < MA60 (+0.5), RSI < 30 (+0.5), hist 0 -> +1.0
    score = technical_score(Decimal("101"), Decimal("100"), Decimal("103"),
                            Decimal("25"), Decimal("0"))
    assert score == Decimal("1.0")


def test_compute_technical_unavailable_below_min_bars():
    signal, indicators = compute_technical([Decimal(100)] * (MIN_BARS - 1))
    assert signal.status == "unavailable" and signal.signal is None
    assert "insufficient price history" in signal.note
    assert indicators is None


def test_compute_technical_uptrend_scores_and_reports_indicators():
    closes = [Decimal(100) + Decimal(i) for i in range(80)]
    signal, indicators = compute_technical(closes)
    assert signal.status == "ok"
    assert Decimal("-2") <= signal.signal <= Decimal("2")
    assert signal.signal > 0  # steady uptrend must not read bearish
    assert indicators["ma20"] == sma(closes, 20)
    assert set(indicators) == {"ma20", "ma60", "rsi14", "macd",
                               "macd_signal", "macd_hist"}


class _TechnicalConn:
    """Captures the technical_indicator upsert (checked against schema: no
    ingested_at column on that table)."""

    def __init__(self, closes: list[Decimal]):
        self._closes = closes
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        assert "FROM price_bar" in query and "ORDER BY bar_date DESC" in query
        return [{"bar_date": None, "close": c} for c in reversed(self._closes)]

    async def execute(self, query, *args):
        self.executed.append((query, args))


async def test_technical_signal_upserts_indicators_and_score():
    closes = [Decimal(100) + Decimal(i) for i in range(80)]
    conn = _TechnicalConn(closes)
    signal, latest_close = await technical_signal(conn, 1, date(2026, 7, 8))
    assert signal.status == "ok"
    assert latest_close == closes[-1]
    query, args = conn.executed[0]
    assert "INSERT INTO technical_indicator" in query
    assert "ON CONFLICT (ticker_id, calc_date)" in query
    assert "ingested_at" not in query  # column doesn't exist on this table
    score = args[-1]
    assert isinstance(score, Decimal)
    assert score == score.quantize(Decimal("0.01"))  # 2dp at persistence


async def test_technical_signal_short_history_returns_close_but_no_upsert():
    conn = _TechnicalConn([Decimal("42")] * 5)
    signal, latest_close = await technical_signal(conn, 1, date(2026, 7, 8))
    assert signal.status == "unavailable"
    assert latest_close == Decimal("42")  # FR-27 technical reference survives
    assert conn.executed == []


# --- chip: TW nets + aux-partial visibility ---------------------------------------

def tw_row(**over):
    row = {"foreign_net": 1_000_000, "investment_trust_net": 50_000,
           "dealer_net": 10_000, "margin_balance": 9_000_000,
           "block_trade_volume": 20_000}
    row.update(over)
    return row


def test_chip_tw_all_institutions_buying_saturates():
    signal = chip_score_tw(tw_row())
    assert signal.signal == Decimal("2.0")  # 1.0 + 0.6 + 0.4
    assert signal.status == "ok" and signal.note is None  # full data, no note


def test_chip_tw_mixed_signs_weighted():
    signal = chip_score_tw(tw_row(foreign_net=-5, investment_trust_net=100,
                                  dealer_net=0))
    assert signal.signal == Decimal("-0.4")  # −1.0 + 0.6 + 0


def test_chip_tw_aux_partial_scores_from_nets_and_names_the_gap():
    # Cindy binding condition + A1 fallback + A6 D2: NULL margin/block (incl.
    # the permanent TPEx-block gap) must not mark chip unavailable, but the
    # partial state must be VISIBLE — never silently treated as full data.
    signal = chip_score_tw(tw_row(margin_balance=None, block_trade_volume=None))
    assert signal.status == "ok"
    assert signal.signal == Decimal("2.0")  # scored from the 3 nets
    assert "3-institution nets only" in signal.note
    assert signal.subfields_complete is False  # v1.2.6 GF-CHIP-PARTIAL
    assert "margin/block unavailable" in signal.note


def test_chip_tw_missing_net_is_named_and_votes_zero():
    signal = chip_score_tw(tw_row(foreign_net=None))
    assert signal.signal == Decimal("1.0")  # 0 + 0.6 + 0.4
    assert "nets partial" in signal.note and "foreign_net" in signal.note


def test_chip_tw_no_nets_at_all_is_unavailable():
    signal = chip_score_tw(tw_row(foreign_net=None, investment_trust_net=None,
                                  dealer_net=None))
    assert signal.status == "unavailable" and signal.signal is None


def test_chip_tw_no_row_is_unavailable():
    assert chip_score_tw(None).status == "unavailable"


# --- chip: US 13F quarterly positioning -------------------------------------------

Q1, Q2 = date(2026, 3, 31), date(2025, 12, 31)


def test_chip_us_no_rows_unavailable():
    assert chip_score_us([]).status == "unavailable"


def test_chip_us_single_quarter_is_neutral_with_note():
    signal = chip_score_us([(Q1, 1_000)])
    assert signal.signal == Decimal("0")
    assert US_LABEL in signal.note  # FR-16 quarterly-positioning label
    assert "no positioning delta" in signal.note


def test_chip_us_delta_scaled_and_clamped():
    # +10% aggregate accumulation -> 10/5 = +2 (saturates)
    assert chip_score_us([(Q1, 1_100), (Q2, 1_000)]).signal == Decimal("2")
    # −5% distribution -> −1
    assert chip_score_us([(Q1, 950), (Q2, 1_000)]).signal == Decimal("-1")
    # +2.5% -> +0.5, label always carried (FR-16)
    signal = chip_score_us([(Q1, 1_025), (Q2, 1_000)])
    assert signal.signal == Decimal("0.5")
    assert US_LABEL in signal.note


def test_chip_us_zero_prior_base_is_direction_only():
    assert chip_score_us([(Q1, 500), (Q2, 0)]).signal == Decimal("2")
    assert chip_score_us([(Q1, 0), (Q2, 0)]).signal == Decimal("0")


# --- news: unavailable-not-neutral -------------------------------------------------

def test_news_empty_is_unavailable_not_neutral():
    # Contract §4 + GF-B/GF-C: a missing module is UNAVAILABLE (feeds
    # renormalisation), never a fabricated neutral 0 (silent dilution).
    signal = news_score([])
    assert signal.status == "unavailable"
    assert signal.signal is None


def test_news_mean_sentiment_scaled_to_signal_range():
    assert news_score(dseq("0.25", "0.25")).signal == Decimal("0.5")  # x2 scale
    assert news_score(dseq("-1", "-1")).signal == Decimal("-2")  # clamped floor


# --- fundamental: peer comparison ---------------------------------------------------

def peer(pe=None, pb=None, net_margin=None):
    return {"pe": None if pe is None else Decimal(str(pe)),
            "pb": None if pb is None else Decimal(str(pb)),
            "net_margin": None if net_margin is None else Decimal(str(net_margin))}


PEERS = [peer(pe=20, pb=3, net_margin="0.10"),
         peer(pe=25, pb=4, net_margin="0.12"),
         peer(pe=30, pb=5, net_margin="0.08")]  # medians: pe 25, pb 4, margin 0.10


def test_median_odd_even_and_empty():
    assert median(dseq(3, 1, 2)) == Decimal("2")
    assert median(dseq(1, 2, 3, 4)) == Decimal("2.5")
    assert median([]) is None


def test_fundamental_cheap_profitable_vs_peers_scores_max():
    row = {"pe": Decimal("18"), "pb": Decimal("3"), "eps": Decimal("40"),
           "net_margin": Decimal("0.20")}
    signal, peer_median_pe, eps = fundamental_score(row, PEERS)
    # pe 18 <= 0.8·25=20 -> +1; pb 3 <= 0.8·4=3.2 -> +0.5; margin above -> +0.5
    assert signal.signal == Decimal("2.0")
    assert signal.note is None
    assert peer_median_pe == Decimal("25")  # feeds FR-27 target
    assert eps == Decimal("40")


def test_fundamental_rich_low_margin_scores_min():
    row = {"pe": Decimal("50"), "pb": Decimal("9"), "eps": Decimal("2"),
           "net_margin": Decimal("0.01")}
    signal, _, _ = fundamental_score(row, PEERS)
    assert signal.signal == Decimal("-2.0")


def test_fundamental_negative_pe_unscored_but_named():
    # Loss-maker: pe is NULL upstream (or ≤ 0) — the vote abstains visibly.
    row = {"pe": None, "pb": Decimal("4"), "eps": Decimal("-5"),
           "net_margin": Decimal("0.10")}
    signal, peer_median_pe, eps = fundamental_score(row, PEERS)
    assert signal.status == "ok"
    assert "pe" in signal.note  # partial inputs named, not hidden
    assert eps == Decimal("-5")  # honest negative EPS (target guard handles it)


def test_fundamental_no_row_is_unavailable_but_peer_median_survives():
    signal, peer_median_pe, eps = fundamental_score(None, PEERS)
    assert signal.status == "unavailable"
    assert peer_median_pe == Decimal("25") and eps is None


def test_fundamental_no_metrics_is_unavailable():
    signal, _, _ = fundamental_score({"pe": None, "pb": None, "eps": Decimal(1),
                                      "net_margin": None}, PEERS)
    assert signal.status == "unavailable"


def test_fundamental_no_peers_scores_only_peerless_votes():
    row = {"pe": Decimal("18"), "pb": Decimal("3"), "eps": Decimal("40"),
           "net_margin": Decimal("0.20")}
    signal, peer_median_pe, _ = fundamental_score(row, [])
    assert peer_median_pe is None
    assert signal.status == "ok" and signal.signal == Decimal("0")
    assert "pe" in signal.note and "pb" in signal.note  # abstentions named


# --- shared helpers -----------------------------------------------------------------

def test_clamp_and_sign_helpers():
    assert clamp_signal(Decimal("2.4")) == Decimal("2")
    assert clamp_signal(Decimal("-9")) == Decimal("-2")
    assert sign_of(None) == 0 and sign_of(0) == 0
    assert sign_of(Decimal("-3")) == -1 and sign_of(7) == 1
    assert ModuleSignal(signal=Decimal("1"), status="ok").available
