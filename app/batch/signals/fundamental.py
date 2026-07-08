"""Fundamental lens (FR-13): valuation + profitability vs peer comparables.

Peer set (contract §8 note: "peer-set definition is config, versioned"):
the OTHER covered tickers' latest fundamental snapshots — the natural MVP
comparable universe for a single-user coverage list. Peer medians are computed
per metric over peers that actually report it; peer_median_PE additionally
requires pe > 0 (a negative/zero PE is not a meaningful valuation reference —
same spirit as the §8 trailing_EPS > 0 guard).

Scoring rubric (A5 choice; contract is silent on the lens-internal split —
see app.batch.signals docstring). Three documented votes, summed, clamped:
- P/E vs peer median (±1.0): own pe > 0 required; pe <= 0.8·median (cheap)
  → +1.0; pe >= 1.2·median (rich) → −1.0; else 0.
- P/B vs peer median (±0.5): same 0.8/1.2 thresholds.
- net margin vs peer median (±0.5): strictly above → +0.5; below → −0.5.
A vote whose inputs are missing contributes 0 and is named in the note —
partial inputs never silently pose as a full-data score (A6 D2).

Unavailable when the ticker has no fundamental row, or the row carries none
of the scored metrics (pe/pb/net_margin all NULL).
"""

from decimal import Decimal
from typing import Any

from app.batch.signals import ModuleSignal, clamp_signal, q2, unavailable

PE_VOTE = Decimal("1.0")
PB_VOTE = Decimal("0.5")
MARGIN_VOTE = Decimal("0.5")
CHEAP_RATIO = Decimal("0.8")   # own metric <= 0.8 x peer median -> cheap
RICH_RATIO = Decimal("1.2")    # own metric >= 1.2 x peer median -> rich


def median(values: list[Decimal]) -> Decimal | None:
    """Decimal median; even count averages the middle pair. None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def _valuation_vote(own: Decimal | None, peer_median: Decimal | None,
                    vote: Decimal) -> Decimal | None:
    """Cheap/rich step vote; None when the comparison is impossible."""
    if own is None or own <= 0 or peer_median is None or peer_median <= 0:
        return None
    if own <= peer_median * CHEAP_RATIO:
        return vote
    if own >= peer_median * RICH_RATIO:
        return -vote
    return Decimal(0)


def fundamental_score(
    row: dict[str, Any] | None,
    peers: list[dict[str, Any]],
) -> tuple[ModuleSignal, Decimal | None, Decimal | None]:
    """Pure core. Returns (signal, peer_median_pe, trailing_eps); the last two
    feed the FR-27 target formula (contract §8) regardless of the score."""
    peer_pes = [p["pe"] for p in peers if p.get("pe") is not None and p["pe"] > 0]
    peer_median_pe = median(peer_pes)
    if row is None:
        return unavailable("no fundamental snapshot"), peer_median_pe, None

    trailing_eps = row.get("eps")
    pe, pb, net_margin = row.get("pe"), row.get("pb"), row.get("net_margin")
    if pe is None and pb is None and net_margin is None:
        return (unavailable("fundamental snapshot carries no scored metrics"),
                peer_median_pe, trailing_eps)

    peer_pbs = [p["pb"] for p in peers if p.get("pb") is not None and p["pb"] > 0]
    peer_margins = [p["net_margin"] for p in peers if p.get("net_margin") is not None]

    gaps: list[str] = []
    total = Decimal(0)

    pe_vote = _valuation_vote(pe, peer_median_pe, PE_VOTE)
    if pe_vote is None:
        gaps.append("pe")
    else:
        total += pe_vote

    pb_vote = _valuation_vote(pb, median(peer_pbs), PB_VOTE)
    if pb_vote is None:
        gaps.append("pb")
    else:
        total += pb_vote

    margin_median = median(peer_margins)
    if net_margin is None or margin_median is None:
        gaps.append("net_margin")
    elif net_margin > margin_median:
        total += MARGIN_VOTE
    elif net_margin < margin_median:
        total -= MARGIN_VOTE

    note = f"partial inputs — unscored: {', '.join(gaps)}" if gaps else None
    return (ModuleSignal(signal=clamp_signal(total), status="ok", note=note),
            peer_median_pe, trailing_eps)


async def fundamental_signal(
    conn: Any, ticker_id: int
) -> tuple[ModuleSignal, Decimal | None, Decimal | None]:
    """Fetch own latest snapshot + peer latest snapshots, score, and persist
    the score onto the snapshot row (the twse/yfinance adapters left `score`
    NULL for task #10 to fill). Returns (signal, peer_median_pe, trailing_eps)."""
    row = await conn.fetchrow(
        "SELECT asof_date, pe, pb, eps, net_margin FROM fundamental"
        " WHERE ticker_id = $1 ORDER BY asof_date DESC LIMIT 1",
        ticker_id,
    )
    peers = await conn.fetch(
        "SELECT DISTINCT ON (f.ticker_id) f.pe, f.pb, f.net_margin"
        " FROM fundamental f JOIN ticker t ON t.id = f.ticker_id"
        " WHERE t.is_covered AND f.ticker_id <> $1"
        " ORDER BY f.ticker_id, f.asof_date DESC",
        ticker_id,
    )
    signal, peer_median_pe, trailing_eps = fundamental_score(
        dict(row) if row is not None else None, [dict(p) for p in peers]
    )
    if signal.signal is not None and row is not None:
        await conn.execute(
            "UPDATE fundamental SET score = $3 WHERE ticker_id = $1 AND asof_date = $2",
            ticker_id, row["asof_date"], q2(signal.signal),
        )
    return signal, peer_median_pe, trailing_eps
