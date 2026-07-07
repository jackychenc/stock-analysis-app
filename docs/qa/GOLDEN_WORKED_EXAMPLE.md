# Golden Worked Example — canonical arithmetic for design / dev / QA

Owner: A6 · Task #5. **Single source of truth** for sample numbers, so @SDLCUXUIDesignerAgent's mocks,
@SDLCSoftwareDeveloperAgent's engine, and my golden fixtures all reference identical arithmetic.
(Resolves the PM-flagged internal inconsistency in dashboard-states.png state B.)

Ticker: **2330.TW** · Default weights **Technical 0.30 / Fundamental 0.30 / Chip 0.25 / News 0.15** · Horizon 6M.
Band edges LOCKED (A2+A3+PM, milder-wins): SS [−2.0,−1.5) · S [−1.5,−0.75) · Hold [−0.75,+0.75] · Buy (+0.75,+1.5] · SB (+1.5,+2.0].
Conflict flag when signal spread **> 2.0** (strictly greater). All money math is `Decimal`.

Fixed lens signals used across all three states (only availability changes):
`Technical +1.2 · Fundamental +0.8 · Chip +1.5 · News −0.5`

---

## STATE A — NORMAL (all 4 scoring modules ok)
```
composite = 0.30·(+1.2) + 0.30·(+0.8) + 0.25·(+1.5) + 0.15·(−0.5)
          = 0.360 + 0.240 + 0.375 − 0.075
          = +0.900
```
- **Call = Buy**  (+0.900 ∈ (+0.75, +1.5])
- **Signal spread** = max − min = (+1.5) − (−0.5) = **2.0** → **not >2.0 → no "Conflicting Signals" flag** (boundary demo).
- **Confidence** = **High** · agreement = survivors whose sign matches composite(+): {+,+,+,−}=3/4=**75%** ≥75 → High · **Reduced Confidence label: NO**
- **data_completeness = 1.00** · every module `weight_effective == weight_assigned`
- **Target price** (formula §8, inputs peer_median_PE=18.0, trailing_EPS=40.0, latest_close=900.0): fundamental_fair_value=720.0 → base=0.5·720+0.5·900 = **Bear 688.50 · Base 810.00 · Bull 931.50**

per_module_breakdown:
| module | signal | weight_assigned | weight_effective | status |
|---|---|---|---|---|
| technical | +1.2 | 0.30 | 0.30 | ok |
| fundamental | +0.8 | 0.30 | 0.30 | ok |
| chip | +1.5 | 0.25 | 0.25 | ok |
| news | −0.5 | 0.15 | 0.15 | ok |

## STATE B — REDUCED CONFIDENCE (Technical unavailable → 1 of 4 down)
Survivors {Fundamental, Chip, News}; assigned survivor weight sum = 0.30+0.25+0.15 = **0.70**. Renormalise: effective = assigned / 0.70.
```
effective (contract §9 rounds to 2dp): fundamental 0.30/0.70=0.4286→0.43 · chip 0.25/0.70=0.3571→0.36 · news 0.15/0.70=0.2143→0.21  (Σ=1.00)
composite = (0.30·0.8 + 0.25·1.5 + 0.15·(−0.5)) / 0.70
          = (0.240 + 0.375 − 0.075) / 0.70
          = 0.540 / 0.70
          = +0.7714  → round-half-up 2dp (contract §3) = +0.77
```
- **Call = Buy**  (+0.77 ∈ (+0.75, +1.5]) — sits just above the Hold/Buy edge (deliberate boundary demo). NOTE: composite is persisted at 2dp (+0.77); the unrounded +0.7714 is shown for derivation only.
- **Signal spread** (survivors) = (+1.5) − (−0.5) = 2.0 → no conflict flag.
- **Confidence** = **Medium** · agreement = survivors matching composite(+): {+,+,−}=2/3=**66.7%** (50–<75) → Medium · **Reduced Confidence label: YES** (Technical missing — label independent of %, per PM OQ-3).
- **data_completeness = 0.75**

per_module_breakdown:
| module | signal | weight_assigned | weight_effective | status |
|---|---|---|---|---|
| technical | — (null) | 0.30 | 0.00 | unavailable |
| fundamental | +0.8 | 0.30 | 0.43 | ok |
| chip | +1.5 | 0.25 | 0.36 | ok |
| news | −0.5 | 0.15 | 0.21 | ok |

## STATE C — SUPPRESSED (Technical + Chip unavailable → ≥2 down)
- **No composite / call / target computed.** UI shows **"Analysis Only — Insufficient Data"**.
- Breakdown still lists all four modules with statuses; **data_completeness = 0.50**.

| module | signal | weight_assigned | weight_effective | status |
|---|---|---|---|---|
| technical | — | 0.30 | — | unavailable |
| fundamental | +0.8 | 0.30 | — | ok |
| chip | — | 0.25 | — | unavailable |
| news | −0.5 | 0.15 | — | ok |

## STATE D — CONFLICT (all 4 ok, modules strongly disagree → wide spread)
Demonstrates the "Conflicting Signals" banner with full data (distinct from degradation).
Signals: `Technical +1.9 · Fundamental −1.5 · Chip +1.7 · News −0.8`
```
composite = 0.30·(+1.9) + 0.30·(−1.5) + 0.25·(+1.7) + 0.15·(−0.8)
          = 0.570 − 0.450 + 0.425 − 0.120
          = +0.425
```
- **Call = Hold**  (+0.425 ∈ [−0.75, +0.75])
- **Signal spread** = (+1.9) − (−1.5) = **3.4 > 2.0 → "Conflicting Signals" flag RAISED**
- **Confidence** = **LOW** · call is HOLD → agreement = survivors that are "Hold-consistent" (|signal| ≤ 0.75) per contract §6; here |1.9|,|1.5|,|1.7|,|0.8| all > 0.75 → **0/4 = 0% → Low**. (A strong-conflict Hold is correctly low-confidence.) · Reduced Confidence label: NO (all present)
- **data_completeness = 1.00**

---

## Confidence formula — FINAL (contract v1.2 §6, A1-approved)
Over surviving scoring modules `A`, `n=|A|`:
- **If call == HOLD** (composite ∈ [−0.75,+0.75]): `agree = |{ m∈A : |signal(m)| ≤ 0.75 }|` ("Hold-consistent"; covers A1 signal==0 and composite==0 edge rules).
- **Else** (Sell/SS/Buy/SB): `s = sign(composite)`; `agree = |{ m∈A : sign(signal(m)) == s }|` (signal==0 does NOT agree).
- `confidence_pct = 100·agree/n` · HIGH ≥75 · MEDIUM [50,75) · LOW <50.
- "Reduced Confidence" LABEL is independent: shown whenever ANY module missing, regardless of %.
Fixtures: TC-CONF-CUT (75/50 cutoffs, else-branch) · TC-CONF-HOLD-BAND (State D: Hold + all strong → 0% LOW) · TC-CONF-ZERO-HOLD (signal==0 in Hold agrees; in Buy/Sell does not) · TC-CONF-COMPOSITE-ZERO (composite==0 → |signal|≤0.75).
**A6 note for A1/A3:** v1.2 generalizes "Hold-consistent = |signal|≤0.75" to the WHOLE Hold band (not just composite==0). This is a reasonable deterministic extension but goes slightly beyond A1's literal rule-2 (composite exactly 0) — flagged for a quick A1 ratify. It changes confidence for strong-conflict Holds (e.g. State D → LOW), which is the correct behaviour.

## Target price — FINAL (contract v1.2 §8 / FR-27, A1-approved)
`target_base = 0.5·(peer_median_PE·trailing_EPS) + 0.5·latest_close` (fundamental available); else `target_base = latest_close` + Reduced Confidence.
**State A worked target** (illustrative inputs): peer_median_PE=18.0, trailing_EPS=40.0 → fundamental_fair_value=720.0; latest_close=900.0 → `target_base = 0.5·720 + 0.5·900 = 810.00` → **Bear 688.50 · Base 810.00 · Bull 931.50**.
**Negative-EPS / no-PE edge (A1 completeness patch → fixture TC-TARGET-NEGEPS):** when `trailing_EPS ≤ 0` OR `peer_median_PE` unavailable, the PE leg is meaningless → fall back to `target_base = latest_close` + Reduced-Confidence framing (never a silent negative target). ⚠ v1.2 §8 explicitly handles *missing* EPS/PE but should also explicitly cover *present-but-≤0* EPS — flagged to A3.

## Still-open
- Signals themselves are inputs (not derived here) — per-lens calculators (FR-12…15) tested separately.

## Provenance / convergence
Canonical set = A6 States A–D (PM standing-rule: "one worked example across UX/dev/QA"). A4's independent patch
(mock-data-worked-examples.md) reached the same deck-defined rules and correctly caught the renormalisation error
(35.3/35.3/29.4 for a News-down case, not 40/40/20); A4's ±1.375 Strong/regular assumption is **superseded** by the
LOCKED ±1.5 edges. A4's State-B (News-down, spread 3.4) is preserved in spirit as State D's conflict demonstration.

## Consumers — please align to THIS file
- @SDLCUXUIDesignerAgent: use States A/B/C numbers verbatim in dashboard-states mock (replaces the inconsistent +0.61 / 2.4-spread values).
- @SDLCSoftwareDeveloperAgent: these are the engine's expected outputs — wire as the first golden unit test.
- A6 fixtures: TC-GOLDEN (State A), TC-DEG-01a + TC-WEIGHT-RENORM (State B), TC-DEG-2plus (State C), TC-CONFLICT (spread=2.0 no-flag edge), TC-BAND-EXACT (Buy just above +0.75).
