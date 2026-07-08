"""Chip / institutional lens (FR-14): TW three-institution nets; US 13F
quarterly positioning (labelled per FR-16).

TW rubric (A5 choice; contract §5 is the band mapping and carries no
lens-internal rubric). The signal computes from the 3-institution NETS under
the adapter's A3 no-double-count convention (see twse_tpex_adapter docstring:
foreign_net excludes foreign dealers; dealer_net includes them). Weighted
direction votes, sign per institution, summed (range exactly [-2, +2]):
- foreign_net          ±1.0 (foreign flows dominate TW price discovery)
- investment_trust_net ±0.6
- dealer_net           ±0.4
margin_balance / block_trade_volume are AUXILIARY inputs (Cindy binding
condition + A1 fallback + A6 D2): when NULL — including the permanent
TPEx-block name-only gap — the lens still scores from the nets, but the
partial-aux state is made visible via the module note; it is NEVER silently
treated as full data, and the lens is NOT marked unavailable while nets exist.
All three nets NULL -> unavailable.

US rubric: aggregate curated-filer shares for the latest 13F quarter vs the
prior quarter; score = clamp(delta_pct / US_PCT_PER_POINT, -2, +2) with
US_PCT_PER_POINT = 5 (a ±10% aggregate positioning swing saturates the
scale). One quarter only -> neutral 0 with an explicit no-delta note (13F is
quarterly + delayed, R-04). No rows -> unavailable.
"""

from decimal import Decimal
from typing import Any

from app.batch.signals import ModuleSignal, clamp_signal, q2, sign_of, unavailable

FOREIGN_WEIGHT = Decimal("1.0")
TRUST_WEIGHT = Decimal("0.6")
DEALER_WEIGHT = Decimal("0.4")

US_PCT_PER_POINT = Decimal("5")  # 5pp aggregate share change per signal point
US_LABEL = "quarterly positioning (13F, delayed)"  # FR-16 honesty label

_AUX_FIELDS = ("margin_balance", "block_trade_volume")
_NET_FIELDS = ("foreign_net", "investment_trust_net", "dealer_net")


def chip_score_tw(row: dict[str, Any] | None) -> ModuleSignal:
    """Pure TW core over one chip_data_tw row (nets + aux)."""
    if row is None:
        return unavailable("no chip facts for latest trading day")
    nets = {name: row.get(name) for name in _NET_FIELDS}
    if all(v is None for v in nets.values()):
        return unavailable("chip row carries no institutional nets")

    score = (FOREIGN_WEIGHT * sign_of(nets["foreign_net"])
             + TRUST_WEIGHT * sign_of(nets["investment_trust_net"])
             + DEALER_WEIGHT * sign_of(nets["dealer_net"]))

    notes = []
    missing_nets = [n for n, v in nets.items() if v is None]
    if missing_nets:
        notes.append(f"nets partial — missing: {', '.join(missing_nets)}")
    missing_aux = [n for n in _AUX_FIELDS if row.get(n) is None]
    if missing_aux:
        # v1.2.6 §9 intra-module completeness (GF-CHIP-PARTIAL binding):
        # scored from nets => status stays ok, NO renormalisation, but
        # subfields_complete=false + a note naming the gap — the disclosed
        # partial state, never posed as full chip data (Cindy/A1/A6 D2).
        notes.append("3-institution nets only; margin/block unavailable")
    return ModuleSignal(signal=clamp_signal(score), status="ok",
                        note="; ".join(notes) or None,
                        subfields_complete=not (missing_nets or missing_aux))


def chip_score_us(quarter_totals: list[tuple[Any, int]]) -> ModuleSignal:
    """Pure US core over [(quarter, total_shares)] sorted latest-first,
    aggregated across curated filers (per-filer rows summed per quarter)."""
    if not quarter_totals:
        return unavailable("no 13F positioning rows")
    if len(quarter_totals) == 1:
        return ModuleSignal(signal=Decimal(0), status="ok",
                            note=f"{US_LABEL}; single quarter — no positioning delta")
    latest, prior = quarter_totals[0][1], quarter_totals[1][1]
    if prior == 0:
        # No prior base: direction only — new positions saturate the scale.
        score = Decimal(2) * sign_of(latest)
    else:
        delta_pct = (Decimal(latest) - Decimal(prior)) / Decimal(prior) * 100
        score = clamp_signal(delta_pct / US_PCT_PER_POINT)
    return ModuleSignal(signal=score, status="ok", note=US_LABEL)


async def chip_signal(conn: Any, ticker_id: int, exchange: str) -> ModuleSignal:
    """Market-routed fetch + score (T9-S3): TW exchanges read chip_data_tw
    (and persist the score the task-#9 adapter left NULL); US reads 13F
    aggregates. The TW score is written onto the row it scored."""
    if exchange in ("TWSE", "TPEx"):
        row = await conn.fetchrow(
            "SELECT trade_date, foreign_net, investment_trust_net, dealer_net,"
            "       margin_balance, block_trade_volume"
            " FROM chip_data_tw WHERE ticker_id = $1"
            " ORDER BY trade_date DESC LIMIT 1",
            ticker_id,
        )
        signal = chip_score_tw(dict(row) if row is not None else None)
        if signal.signal is not None and row is not None:
            await conn.execute(
                "UPDATE chip_data_tw SET score = $3"
                " WHERE ticker_id = $1 AND trade_date = $2",
                ticker_id, row["trade_date"], q2(signal.signal),
            )
        return signal

    rows = await conn.fetch(
        "SELECT quarter, sum(shares) AS total_shares"
        " FROM institutional_position_us WHERE ticker_id = $1 AND shares IS NOT NULL"
        " GROUP BY quarter ORDER BY quarter DESC LIMIT 2",
        ticker_id,
    )
    return chip_score_us([(r["quarter"], int(r["total_shares"])) for r in rows])
