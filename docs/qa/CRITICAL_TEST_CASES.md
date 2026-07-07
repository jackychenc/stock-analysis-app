# Critical Test Cases — Stock Investment Analysis App (v0.1)

Owner: A6 · Task #5. These are the highest-value, must-automate cases (Cindy's explicit list + boundary hardening).
Each maps to a gate dimension (Dx) and will get a requirement ID once A2 posts the RTM.

## A. Module-failure & degradation (D2, D4)
- **TC-DEG-00** — 0 of 4 modules down → full recommendation; all four status=ok; completeness=1.0; weight_effective==weight_assigned; confidence unaffected by degradation.
- **TC-DEG-01a** — 1 module down (e.g. Technical) → recommend on 3 survivors; weights renormalised (30/30/25/15 → survivors sum to 1.0); flagged "Reduced Confidence"; completeness=0.75; failed module status=unavailable in breakdown.
- **TC-DEG-01b** — each of the 4 modules individually down (parametrized ×4) → renormalisation correct for whichever one drops.
- **TC-DEG-2plus** — ≥2 modules down → recommendation SUPPRESSED → "Analysis Only — Insufficient Data"; no composite score shipped; breakdown still lists all 4 with statuses.
- **TC-DEG-EDGE** — exactly 2 down (boundary between renormalise vs suppress) → suppress (≥2 rule inclusive).
- **TC-SUPPRESS-SHAPE** (schema BLOCK, D2+D3) — a SUPPRESSED row MUST have NULL composite_signal/composite_score/target_bear/base/bull and a non-null `suppressed_reason` ("Insufficient Data"); the DB CHECK constraint MUST reject (a) a suppressed row carrying a non-null score and (b) a non-suppressed row with a NULL composite. Guards against A3 v1.0's `composite_signal NOT NULL` forcing a fake score onto Analysis-Only rows — the silent-degradation failure mode. Requires A3 v1.0.1 contract patch.
- **TC-WEIGHT-RENORM** — assert weight_effective values numerically: with Technical(30) down, remaining {Fund 30, Chip 25, News 15}=70 → effective {0.4286, 0.3571, 0.2143}; sums to 1.0 within Decimal tolerance.

## B. Recommendation math & boundaries (D1)
- **TC-BAND-EXACT** — LOCKED band edges (A2+A3+PM, ±1.5 symmetric, boundary → milder call):
  Strong Sell [−2.0,−1.5) · Sell [−1.5,−0.75) · Hold [−0.75,+0.75] · Buy (+0.75,+1.5] · Strong Buy (+1.5,+2.0].
  Boundary cases (milder wins): −1.5→Sell, −0.75→Hold, +0.75→Hold, +1.5→Buy. Test both sides of each edge (±0.001).
- **TC-CONF-CUT** — module-agreement at exactly 50% and 75% → Low/Medium/High boundary (High ≥75, Medium 50–<75, Low <50). Formula APPROVED (PM OQ-CONF): agreement = % surviving scoring modules whose signal sign matches composite sign. "Reduced Confidence" label independent: shown whenever ANY module missing, regardless of % (PM OQ-3).
- **TC-CONF-HOLD-BAND** (contract v1.2 §6) — for ANY Hold call, agreement = % survivors with |signal| ≤ 0.75. Golden = State D: composite +0.425 Hold, signals 1.9/−1.5/1.7/−0.8 all |·|>0.75 → 0/4 = 0% → LOW (strong-conflict Hold is low-confidence).
- **TC-CONF-ZERO-HOLD** (Edge-1) — a module signal of exactly 0: agrees when call==Hold (|0|≤0.75), does NOT agree when composite is Buy/Sell/Strong (sign(0)≠±1).
- **TC-CONF-COMPOSITE-ZERO** (Edge-2) — when composite == 0 (→ Hold band), agreement = % survivors with |signal| ≤ 0.75.
- ⚠ **Ratify-flag:** v1.2 applies |signal|≤0.75 to the WHOLE Hold band, not only composite==0 — confirm A1 accepts this generalization (changes confidence for non-zero Holds like State D).
- **TC-CONFLICT** — score spread exactly 2.0 (no flag) vs >2.0 (flag "Conflicting Signals").
- **TC-TARGET** — Base = 0.5·(peer_median_PE·trailing_EPS) + 0.5·latest_close (contract §8); Bear=Base×0.85, Bull=Base×1.15, round 4dp, Decimal-exact. Golden: PE=18, EPS=40, close=900 → base 810.00 → Bear 688.50 / Bull 931.50.
- **TC-TARGET-NEGEPS** (A1-requested completeness fixture) — when `trailing_EPS ≤ 0` (present but non-positive, e.g. growth/biotech) OR `peer_median_PE` unavailable → PE leg meaningless → target_base = latest_close (technical-only) + Reduced-Confidence framing; assert NO negative/silent target ships. (v1.2 §8 covers *missing* EPS/PE; this fixture also pins *present-but-≤0* EPS — flagged to A3.)
- **TC-GOLDEN** — a set of hand-computed full scenarios reproduce stored composite_score, targets, confidence, breakdown byte-for-byte.

## C. API / dashboard contract (D3)
- **TC-DASH-4KEYS** — dashboard response ALWAYS contains all 4 module keys, even when unavailable/suppressed.
- **TC-DASH-BREAKDOWN** — per_module_breakdown non-empty; each item has {module, signal_score, weight_assigned, weight_effective, status}; no recommendation ships with empty reasoning.
- **TC-HTTP-200-FLAG** — single module failure → HTTP 200 with that module flagged (NOT 503).
- **TC-HTTP-503** — total API/DB outage → 503; and ONLY total outage yields 503 (partial degradation never 503).
- **TC-SECTOR** — out-of-scope ticker → SECTOR_NOT_COVERED (not 404/500).
- **TC-AUTH-GATE** — every §21 route returns 401 unauthenticated.

## D. Backtest (D5)
- **TC-BT-BENCH** — Buy flagged correct only if ticker beats benchmark (^TWII/.TW·.TWO, ^GSPC/US) by >2pp over horizon; Sell correct if lags; Hold excluded from accuracy.
- **TC-BT-PARTIAL** — partial-data days tagged and EXCLUDED from headline rolling_accuracy; reported as separate stat.
- **TC-BT-HISTORY** — <12mo history → "insufficient history" (no misleading number); ≥12mo → computes.
- **TC-BT-VERSION** — methodology_version stored on each backtest_result; changing methodology yields new version, old rows unchanged.

## E. Immutability & decision log (D6)
- **TC-IMMUT** — attempt to mutate a recommendation row → rejected/append-only; history preserved.
- **TC-DECISION** — user_decision_log annotation ('followed'/'ignored'/'partial' + transaction_price + notes) links via FK to recommendation and does NOT alter the recommendation.

## F. Auth (D7)
- **TC-AUTH-WEB** — cookie session issue + validate happy path; expired/absent cookie → 401.
- **TC-AUTH-IOS** — short-lived JWT validate + refresh flow; tampered/expired JWT → 401.

## G. Security-adjacent & data (D10, D15)
- **TC-ENC-REST** — transaction_price + decision log ciphertext in DB at rest (no plaintext); decrypt path works for authorised read.
- **TC-NO-LEAK** — no secrets/connection strings in repo/image/logs; PII not written to app logs.
- **TC-DECIMAL** — money math uses Decimal end-to-end; no float rounding drift across score→target persistence→read.
- **TC-SYMBOL** — .TW, .TWO, US resolve to correct exchange; full_symbol UNIQUE enforced.

## H. Observability & disclaimer (D14, D11)
- **TC-PIPESTATUS** — /pipeline/status reports per-source ok/unavailable for the last run.
- **TC-ALERT-2DAYS** — 2 consecutive failed pipeline days triggers alert (R-01 mitigation).
- **TC-DISCLAIMER** — FR-39 disclaimer present on every recommendation surface (web + iOS).

## Priority note
A–E are P0 (block G2). F–H are P1 (block/condition G3). All A–C cases are pure/deterministic and should be the
first automated suite the moment A3 freezes the breakdown JSONB + band-edge semantics.
