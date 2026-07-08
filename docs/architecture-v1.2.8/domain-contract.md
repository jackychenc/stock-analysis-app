# Domain / Recommendation-Engine Contract v1.0

Owner: A3. Companion to `openapi.yaml` + `schema.sql`. Freezes the deterministic scoring semantics so A5 implements one way and A6 writes golden fixtures against exact edges. Traceability to A2 Requirements Baseline v0.1 (FR/NFR IDs) noted inline.

Legend for ranges: `[` / `]` inclusive, `(` / `)` exclusive.

---

## 1. Module signals
Each of the 4 scoring modules produces `signal_score ∈ [-2.00, +2.00]` (NUMERIC(4,2)) or is `unavailable`. Supply-chain is **discovery only** and never contributes (FR: supply-chain excluded).

## 2. Effective weights & renormalisation (FR-34/35, deck §22.4)
Assigned weights default `{technical:0.30, fundamental:0.30, chip:0.25, news:0.15}` (user-configurable, must sum to 1.0 ±0.001).

Let `A` = set of **available** scoring modules. For each module `m`:
```
weight_effective(m) = weight_assigned(m) / Σ_{k∈A} weight_assigned(k)      for m ∈ A
weight_effective(m) = 0                                                      for m ∉ A
```
When `A` = all 4, effective == assigned. Both are always persisted in `per_module_breakdown` (FR-37).

**Precision ruling (A3, resolves A6's fake-store 6dp vs example note):** `weight_effective` is used at **full precision for computation** (no intermediate rounding — compute the composite, then round *that*, per §3). For **storage/display** in `per_module_breakdown`, round `weight_effective` to **4 decimal places** (matches the canonical GOLDEN_WORKED_EXAMPLE: 0.4286/0.3571/0.2143). Do NOT round to 2dp for compute — that drifts the composite. Engine (task #10) and QA fixtures both bind to: full-precision compute, 4dp persisted.

`data_completeness = |A| / 4` (0.25 increments).

## 3. Composite score
```
composite_signal = Σ_{m∈A} weight_effective(m) · signal_score(m)      ∈ [-2, +2]
```
Rounded to 2 decimals (NUMERIC(4,2)), round-half-up.

## 4. Missing-data behaviour (FR-34/35, BLOCK-gate per A6 D2 — no silent degradation)
| # modules down | Behaviour |
|---|---|
| 0 | Normal call. `reduced_confidence=false`. |
| 1 | Renormalise over 3; `reduced_confidence=true`; list missing module; completeness 0.75. |
| ≥2 | **SUPPRESS**: `composite_call=SUPPRESSED`, no score/target/confidence; `suppressed_reason="Analysis Only — Insufficient Data"`; still return available lenses' breakdown for transparency. |

### 4a. Module `unavailable` vs `ok`-but-empty (A3 RULING, per A6 request — news analog of chip nets-only)
A module counts as **`unavailable`** (excluded → §2 renormalise) ONLY when it could not produce a signal — a **fetch/source failure**. A successful fetch that legitimately yields a **neutral** result is **`ok`**, contributes its signal, and does NOT renormalise. The boundary is source-availability, not result-magnitude. Concretely:
- **News (informational), FETCH FAILURE / source down / adapter error** ⇒ `status:unavailable`, `signal_score:null`, excluded, **renormalise** (do NOT substitute a fake neutral 0.0 — that would fabricate a signal from missing data).
- **News, FETCH SUCCEEDED but 0 relevant headlines** ⇒ `status:ok`, `signal_score:0.00` (genuinely neutral — "no news is neutral news"), `subfields_complete:true`, and a headline-count of 0 surfaced (e.g. `subfields_note:"0 headlines in window"` or a count field). Contributes 0.00 to the composite; **no renormalisation**.
- Same principle applies to any module: distinguish "couldn't fetch" (unavailable) from "fetched, legitimately empty/neutral" (ok). Adapters MUST model these as distinct states from day one.
- This is the **opposite completeness outcome** to chip nets-only (§9 `subfields_complete`): chip-partial stays `ok` with a *non-null* signal; news-empty stays `ok` with a *0.0* signal; news-fetch-fail goes `unavailable`. All three are honest, none silently degrade.

## 5. 5-point call — band mapping (FR-26) — **A3 RULING**
Deck fixes the **Hold** band `[-0.75, +0.75]` and scale extremes `±2.0`, but does **not** print the Strong-vs-regular boundaries.

**Edge-inclusivity ruling (A3, definitive):** Hold owns both its edges; adjacent bands are open at the Hold side. This makes exactly `-0.75` and `+0.75` resolve to **HOLD** (the conservative choice for a decision-support tool).

**Internal split ±1.5 — ACCEPTED (A2 locked FR-26 v0.1, A1 seconded, 2026-07-08):**
```
STRONG_SELL : [-2.00, -1.50)
SELL        : [-1.50, -0.75)
HOLD        : [-0.75, +0.75]
BUY         : (+0.75, +1.50]
STRONG_BUY  : (+1.50, +2.00]
```
Verified against A2 Gherkin US-3: −1.6→Strong Sell, −1.0→Sell, 0→Hold, +1.0→Buy, +1.6→Strong Buy. The ±1.5 value lives in engine config (`methodology`), not schema. A6 D1: use these exact edges.

## 6. Confidence (FR-28) — **RESOLVED (A1 approved 2026-07-08; A3 encoded)**
`confidence_pct` = **directional agreement** over the surviving (available) scoring modules `A`, `n = |A|`:
```
if composite_call == HOLD:                      # Hold band, composite ∈ [-0.75, +0.75]
    agree = |{ m∈A : |signal(m)| <= 0.75 }|     # "Hold-consistent"; covers signal==0 (A1 edge rule 1)
                                                # and composite==0 case (A1 edge rule 2)
else:                                           # composite maps to Sell/Strong-Sell/Buy/Strong-Buy
    s = sign(composite)                         # +1 or −1
    agree = |{ m∈A : sign(signal(m)) == s }|    # signal==0 does NOT agree (A1 edge rule 1)
confidence_pct = 100 · agree / n
```
Level: `HIGH ≥ 75%` · `MEDIUM [50%,75%)` · `LOW < 50%` (deck §6).
> A1 edge rules verbatim: (1) a module signal of exactly 0 agrees only when the composite maps to Hold; (2) when composite is exactly 0, agreement = % of survivors with |signal| ≤ 0.75. Encoded above by treating the whole Hold band as "Hold-consistent = |signal| ≤ 0.75" (deterministic generalization of rule 2; confirm w/ @SDLCQATestEngineerAgent golden fixtures).

## 7. Conflict flag (deck §6, FR)
```
spread = max_{m∈A} signal(m) − min_{m∈A} signal(m)
conflict_flag = spread > 2.0
```
Shown with every non-suppressed recommendation.

## 8. Target price (FR-27, deck §6, ±15% MVP) — **RESOLVED (A1 pre-approved formula 2026-07-08)**
Deterministic given the day's facts (FR-19), explainable, versioned (methodology_version, FR-31), and honest under missing fundamentals (A1 constraint).
```
# Two reference prices from the day's facts:
fundamental_fair_value = peer_median_PE · trailing_EPS        # from fundamental facts (S4)
technical_reference    = latest_close                         # last price_bar close (S3)

# The TARGET's fundamental leg is "usable" only if the PE-based value is meaningful.
# Guard (A1 FR-27 completeness patch): treat it as UNAVAILABLE when
#   trailing_EPS <= 0  OR  peer_median_PE is null  — EVEN IF the fundamental *scoring*
#   module is otherwise ok. (Negative-EPS growth/biotech would else yield a dishonest
#   negative target.) In that case fall back to technical-only + Reduced-Confidence framing.
target_fundamental_usable = (fundamental scoring available) AND (trailing_EPS > 0) AND (peer_median_PE is not null)

# Blend (weights are engine-config under methodology_version; MVP default 0.5/0.5):
if target_fundamental_usable:
    target_base = round(w_f · fundamental_fair_value + w_t · technical_reference, 4)   # w_f=w_t=0.5
else:
    target_base = round(technical_reference, 4)              # technical-only; row carries Reduced
                                                            # Confidence framing. Never a silent thinner
                                                            # blend, never a negative PE-derived target.
                                                            # A6: golden fixture for trailing_EPS<=0.
target_bear = round(target_base · 0.85, 4)                   # fixed −15% band (MVP)
target_bull = round(target_base · 1.15, 4)                   # fixed +15% band (MVP)
```
The per-lens breakdown records which references fed `target_base` (explainability). If peer set or EPS is missing, `fundamental_fair_value` is undefined and the technical-only branch applies. Blend weights + peer-set definition are config, versioned — future tuning is a `methodology_version` bump, not a contract change.

## 9. `per_module_breakdown` JSONB — frozen shape (FR-37)
Array, **always non-empty** (CHECK in schema; contract in OpenAPI `PerModuleBreakdown`). Example = the canonical GOLDEN_WORKED_EXAMPLE **State B** (2330.TW, Technical unavailable) — numbers verified: effective weights renormalise the surviving {fund 0.30, chip 0.25, news 0.15}=0.70, composite = 0.4286·0.8 + 0.3571·1.5 + 0.2143·(−0.5) = **+0.7714 → Buy**:
```json
[
  { "module": "technical",   "signal_score": null, "weight_assigned": 0.30, "weight_effective": 0.0000, "status": "unavailable" },
  { "module": "fundamental", "signal_score": 0.80, "weight_assigned": 0.30, "weight_effective": 0.4286, "status": "ok" },
  { "module": "chip",        "signal_score": 1.50, "weight_assigned": 0.25, "weight_effective": 0.3571, "status": "ok" },
  { "module": "news",        "signal_score": -0.50,"weight_assigned": 0.15, "weight_effective": 0.2143, "status": "ok" }
]
```
`module ∈ {technical,fundamental,chip,news}` (fixed order recommended). Unavailable ⇒ `signal_score:null`, `weight_effective:0.0000`. `weight_effective` persisted at 4dp (§2 precision ruling).

**Intra-module completeness (v1.2.6, `subfields_complete`):** distinct from module-level availability (§1/§4). A module is `status:ok` and contributes its signal whenever it can compute one from its available sub-fields — even if some sub-fields are missing. `subfields_complete` (default true) flags that partial state:
- **chip nets-only** (3-institution nets present, `margin`/`block` NULL or the TPEx-block gap) ⇒ `status:ok`, `subfields_complete:false`, `subfields_note:"3-institution nets only; margin/block unavailable"`. Chip STILL contributes (no renormalisation, `data_completeness` module-level unchanged) — the gap is disclosed, never presented as full-data (A8 compliance / FR-37).
- A module with all expected sub-fields ⇒ `subfields_complete:true`.
- **Do NOT** conflate `subfields_complete:false` with module `unavailable` — the former stays in the composite, the latter triggers §2/§4 renormalisation. (Pinned by A6 fixture `GF-CHIP-PARTIAL-intra-module`: chip ok, signal non-null, data_completeness 1.00, renormalisation_triggered false, partial flagged in breakdown.)

## 10. Backtest honesty (FR-40/41/42/43, NFR-13; BLOCK-gate per A6 D5)
- **Correct Buy**: `stock_return − benchmark_return > +2.0pp` over horizon. **Correct Sell**: `< −2.0pp`. **Hold excluded** from accuracy.
- Benchmark: `^TWII` (TW) / `^GSPC` (US), matched to the ticker's market.
- `rolling_accuracy_full` computed over **full-data days only**; partial-data days reported in `rolling_accuracy_partial`, **never blended** into the headline.
- History gate: `< 12 months` ⇒ `insufficient_history=true`, `rolling_accuracy_full=null` (no misleading number).
- `methodology_version` stamped on every `recommendation` and `backtest_result` (immutability + reproducibility, ADR-003).

---
### Domain open items — ALL RESOLVED 2026-07-08
- **OD-1** — ±1.5 Strong/regular split → ✅ RESOLVED (A2 locked FR-26, A1 seconded). §5.
- **OD-2** — `target_base` valuation blend (FR-27) → ✅ RESOLVED (A1 pre-approved formula). §8.
- **OD-3** — confidence formula (FR-28) → ✅ RESOLVED (A1 approved + edge rules). §6.
Engine (task #10) now has a fully deterministic scoring/target/confidence/backtest contract; @SDLCQATestEngineerAgent golden fixtures + @SDLCSoftwareDeveloperAgent implementation can both bind to this doc. Config-level tuning (band split value, blend weights, peer set) rides `methodology_version` — no contract change.
