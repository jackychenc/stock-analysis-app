"""Signal calculators (task #10) — one module per scoring lens (FR-12…FR-15).

Each calculator produces a full-precision Decimal signal in [-2, +2] or an
honest `unavailable` (domain-contract §1). Rounding to the persisted 2dp
happens ONLY at the persistence edge (contract §2 precision ruling) — never
inside the calculators or the composite math.

The contract (§5) fixes the composite band mapping but is deliberately silent
on each lens's internal rubric ("config-level tuning rides
methodology_version"). The rubrics here are therefore documented A5 choices
with named constants — deterministic given the day's facts (FR-19).
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

SIGNAL_MIN = Decimal("-2")
SIGNAL_MAX = Decimal("2")


@dataclass(frozen=True)
class ModuleSignal:
    """One lens's output: full-precision signal or unavailable (contract §1).

    `note` is the visible-partial-completeness channel (A6 D2 — no silent
    degradation): e.g. the chip lens scoring from nets while margin/block aux
    facts are NULL must say so rather than pose as a full-data score.
    """

    signal: Decimal | None  # full precision; None <=> unavailable
    status: str  # "ok" | "unavailable"
    note: str | None = None
    # v1.2.6 §9: intra-module completeness — distinct from availability.
    # False only when the lens scored from a subset of its expected
    # sub-fields (MVP: chip nets-only / TPEx-block gap).
    subfields_complete: bool = True

    @property
    def available(self) -> bool:
        return self.signal is not None


def unavailable(note: str | None = None) -> ModuleSignal:
    return ModuleSignal(signal=None, status="unavailable", note=note)


def clamp_signal(value: Decimal) -> Decimal:
    """Clamp into the contract §1 range [-2, +2] (full precision)."""
    return max(SIGNAL_MIN, min(SIGNAL_MAX, value))


def q2(value: Decimal) -> Decimal:
    """Persistence rounding: 2dp, round-half-up (contract §3)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def sign_of(value: Decimal | int | None) -> int:
    """Ternary sign; None counts as 0 (no directional vote)."""
    if value is None or value == 0:
        return 0
    return 1 if value > 0 else -1
