# Software Requirements Specification — Functional Requirements (SRS/FR)
**Version:** v0.1 · **Author:** A2 SDLCRequirementsBAAgent · **Date:** 2026-07-08
**Source:** `stock_app_system_design_20260707_163810.pptx` v1.0 · **Companion:** `03_NFR.md`, `05_user_stories_gherkin.md`, `06_RTM.md`

**Convention:** Each FR has a stable ID `FR-NN`. Deck-cited IDs are preserved (FR-39 = persistent disclaimer). Every Gherkin scenario in `05_user_stories_gherkin.md` is tagged with the FR ID(s) it verifies, for RTM auto-linking (per A6 QA request). Priority uses MoSCoW (M/S/C/W).

---

## A. Authentication & Session (FR-01…FR-05)
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-01 | All API routes except auth endpoints SHALL be auth-gated; unauthenticated requests return 401. | M | §21 |
| FR-02 | Web client SHALL authenticate via session cookie; iOS client SHALL authenticate via short-lived JWT + refresh token. | M | §15 KDD-2, §9 |
| FR-03 | A single FastAPI credential verifier SHALL validate both token strategies. | M | §15 KDD-2 |
| FR-04 | The system SHALL provide login and token-refresh endpoints. | M | §21 |
| FR-05 | Sessions/JWTs SHALL expire; expired credentials return 401 and require re-auth (iOS via refresh token). | M | §15 |

## B. Ticker Lookup & Stock Data (FR-06…FR-11)
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-06 | The user SHALL query any TWSE (`.TW`), TPEx (`.TWO`), or US ticker on demand. | M | §4.1 |
| FR-07 | A read SHALL only touch the last precomputed daily snapshot (no live computation on the request path). | M | §4, §12 |
| FR-08 | Ticker not covered by the system SHALL return `SECTOR_NOT_COVERED` (not a generic error). | M | §21 |
| FR-09 | The system SHALL expose GET endpoints for a ticker's price bars, technical indicators, fundamentals, chip/institutional data, and news items. | M | §21, §20 |
| FR-10 | Stock-data responses SHALL be sourced from the versioned daily snapshot and carry the snapshot/methodology version. | M | §12, §20 |
| FR-11 | Each per-ticker data source's availability SHALL be individually recorded (ok / unavailable) for that snapshot. | M | §8, §22 |

## C. Scoring Lenses / Signal Modules (FR-12…FR-19)
Each scoring module derives a **−2…+2** signal from the day's facts.
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-12 | **Technical** module SHALL compute a −2…+2 signal from price/volume indicators (MA20, MA60, RSI14, MACD). | M | §5 |
| FR-13 | **Fundamental** module SHALL compute a −2…+2 signal from valuation & growth metrics (P/E, P/B, EV/EBITDA; revenue, EPS, margins; comparables). | M | §5 |
| FR-14 | **Chip/institutional** module SHALL compute a −2…+2 signal: TW = three-institution net, margin, block trades; US = 13F quarterly positioning. | M | §5 |
| FR-15 | **Informational** module SHALL compute a −2…+2 signal from GDELT news headlines + VADER sentiment; SHALL default to **neutral** when news is unavailable. | M | §5, §14 |
| FR-16 | US chip signal SHALL be labelled "quarterly positioning" to reflect 13F quarterly + delayed nature. | S | §14 R-04 |
| FR-17 | Each module output SHALL persist its raw signal, assigned weight, effective weight, and status (ok/unavailable). | M | §7, §11, §20 |
| FR-18 | The forward horizon (default 6M; configurable 3/6/12M) SHALL be applied uniformly across all modules. | M | §6 |
| FR-19 | Signal computation SHALL be deterministic given the same input facts (reproducible for backtest & test golden fixtures). | M | §22 |

## D. Supply-Chain Discovery Lens (FR-20…FR-23)
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-20 | The supply-chain lens SHALL surface related investable names from a silicon-wafer graph (fabs, upstream suppliers, downstream customers). | M | §5, §10 |
| FR-21 | The supply-chain lens SHALL be **discovery only** and SHALL NOT feed the composite score. | M | §5, §6 |
| FR-22 | The supply-chain graph SHALL be manually curated and editable **without a deploy** (seed data / config). | S | §14 R-02, §17 |
| FR-23 | The system SHALL expose an endpoint returning supply-chain related names for a queried ticker. | M | §17, §21 |

## E. Recommendation Engine (FR-24…FR-33)
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-24 | The engine SHALL compute a composite −2…+2 score as the weighted blend of the 4 scoring modules (default weights Technical 30 / Fundamental 30 / Chip 25 / News 15). | M | §6 |
| FR-25 | Module weights SHALL be user-configurable and SHALL always sum to 100% (validated). | M | §6, §15 |
| FR-26 | The composite score SHALL map to a 5-point call using these bands (Strong/regular split locked at **±1.5**, reconciled with A3 OD-1 2026-07-08): **Strong Sell** [−2.0, −1.5) · **Sell** [−1.5, −0.75) · **Hold** [−0.75, +0.75] · **Buy** (+0.75, +1.5] · **Strong Buy** (+1.5, +2.0]. Hold band and ±2.0 extremes per deck; edge inclusivity per this row (A3 to encode in engine config). | M | §6 |
| FR-27 | The engine SHALL compute a target price: **Bear** = Base −15%; **Bull** = Base +15% (fixed ±15% band, MVP). **Base formula (A3-resolved OD-2, domain-contract v1.2 §8; weights = engine-config under `methodology_version`):** `target_base = 0.5·(peer_median_PE · trailing_EPS) + 0.5·latest_close` when fundamentals available; if the fundamental leg is down → technical-only `target_base = latest_close` **and the row carries Reduced Confidence** (fundamental is a scoring module, so its absence triggers renormalisation — never a silent thinner blend). **Negative-EPS/invalid-PE guard (A3 v1.2.1 §8, A1-caught):** `target_fundamental_usable = fundamental-scoring-available AND trailing_EPS > 0 AND peer_median_PE ≠ null`; when false → `target_base = latest_close` (technical-only) + Reduced-Confidence framing, **even if the fundamental scoring module is otherwise ok** — no silent negative PE-derived target. Deterministic (FR-19), explainable, versioned (FR-31). | M | §6 |
| FR-28 | The engine SHALL compute **confidence** = **recomputed from surviving-module agreement** (A1 OQ-3): High ≥75%, Medium 50–75%, Low <50%. **Agreement formula (A1-RATIFIED 2026-07-08; contract v1.2.2 §6 stands):**
- If composite maps to **Hold** (composite ∈ [−0.75, +0.75]): `agreement% = % of surviving scoring modules with \|signal\| ≤ 0.75` ("Hold-consistent"). *Rationale (A1): a Hold is a non-directional call, so "does the module's sign match?" is the wrong question; "is the module Hold-consistent?" is the right one. This removes the discontinuity near zero and subsumes the exactly-0 edge rule. Consequence (correct, QA State D): a Hold born of strong opposing signals scores 0/4 → **LOW** confidence + `conflict_flag` — a conflicted Hold showing Medium/High would be misleading.*
- Else (**non-Hold** / directional call): `agreement% = % of surviving scoring modules whose signal sign matches the composite sign`; a signal of exactly 0 does NOT agree.
Golden fixtures cover all edges. **Encoded by A3 in domain-contract v1.2.2 §6.** Confidence **%** and the "Reduced Confidence" **label** are independent signals (see FR-34). | M | §6 |
| FR-29 | The engine SHALL raise a **"Conflicting Signals"** flag when the signal spread across the surviving scoring modules **exceeds** 2.0 (strict `>`). **Spread definition (A2 confirms A6):** `spread = max(surviving scoring signals) − min(surviving scoring signals)`. Spread of exactly 2.0 → **no** flag. | M | §6 |
| FR-30 | Every recommendation SHALL carry a **non-empty per-module breakdown** (module, signal_score, weight_assigned, weight_effective, status). No call ships without reasoning. | M | §11 |
| FR-31 | The recommendation SHALL record a `data_completeness` fraction and `methodology_version`. | M | §10, §11 |
| FR-32 | The composite/confidence math SHALL be computed over the 4 scoring lenses only (supply-chain excluded). | M | §5, §6 |
| FR-33 | Recommendations SHALL be stored as an **immutable** history log keyed by (ticker, date); annotations SHALL never mutate them. | M | §10, §15 KDD-3 |

## F. Missing-Data Degradation (FR-34…FR-37)
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-34 | When **0–1** scoring module is unavailable, the engine SHALL recommend on the survivors, **renormalise** weights, and mark **Reduced Confidence**. Per A1 OQ-3: the "Reduced Confidence" label SHALL be shown **whenever any module is missing, regardless of the recomputed confidence %**. | M | §7, §13 |
| FR-35 | When **≥2** scoring modules are unavailable, the engine SHALL **suppress** the recommendation and show **"Analysis Only — Insufficient Data"**. In the SUPPRESSED state **no composite score, call, or target price SHALL be issued** — composite_signal/score and target fields are null/absent and a `suppressed_reason` SHALL be recorded. DB CHECK SHALL permit this state and prevent accidental display of a score. (aligns A3 contract v1.0.1) | M | §7, §13 |
| FR-36 | The per-lens breakdown SHALL always store both `weight_assigned` and `weight_effective`, plus the data-completeness fraction. **Precision (A3 contract v1.2.2 §2/§9):** `weight_effective` uses **full precision for computation**, **4-decimal-place for store/display** (e.g. 0.4286/0.3571/0.2143); composites are unaffected (computed full-precision). Composite score store/display = 2dp round-half-up; target prices = 4dp. | M | §7, §11 |
| FR-37 | Degradation SHALL never be silent — the UI SHALL always indicate reduced/suppressed state and which module(s) are unavailable. | M | §7, §13 |

## G. Backtest (FR-40…FR-44)  *(FR-38/39 reserved below)*
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-40 | The system SHALL compute a **benchmark-relative** backtest metric vs ^TWII (TW) / ^GSPC (US). | M | §7 |
| FR-41 | A call SHALL be scored **correct** if it beats the benchmark by **>2pp** over the horizon; Sell if it lags; **Hold excluded** from scoring. | M | §7 |
| FR-42 | Backtest SHALL require **≥12 months** of history; otherwise it SHALL show "insufficient history" (never a misleading figure). | M | §7 |
| FR-43 | Backtest SHALL segment full-data days from partial-data days; partial-data days SHALL be reported separately and **never blended** into the headline number. | M | §7, §13 |
| FR-44 | Backtest results SHALL persist window_months, rolling_accuracy, estimated_return, methodology_version. | M | §10 |

## H. Configuration (FR-45…FR-47)
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-45 | The user SHALL configure module weights (persisted as `module_weights` JSONB); changes SHALL apply to subsequent scoring. | M | §6, §10 |
| FR-46 | The user SHALL configure the horizon (3/6/12M; default 6M). | M | §6, §10 |
| FR-47 | Configuration endpoints SHALL validate inputs (weights sum to 100%, horizon in {3,6,12}). | M | §21 |

## I. Decision Log (FR-48…FR-51)
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-48 | The user SHALL log a decision against a recommendation: `followed` \| `ignored` \| `partial`. | M | §10 |
| FR-49 | The decision log SHALL optionally capture `transaction_price` (nullable) and free-text `notes`, with `logged_at`. | M | §10 |
| FR-50 | The decision log SHALL reference the specific recommendation (ticker_id, recommendation_date) and SHALL NOT mutate it. | M | §10, §15 KDD-3 |
| FR-51 | `transaction_price` and decision-log entries SHALL be treated as personal financial data (encryption at rest — see NFR-05). | M | §10, §15 KDD-4 |

## J. Batch Pipeline & Status (FR-52…FR-56)
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-52 | An APScheduler batch SHALL run once daily ~03:00 TW, ingesting all sources, computing signals, scoring, and backtesting. | M | §8, §12 |
| FR-53 | Each adapter failure SHALL be isolated (try/except → ok/unavailable) and SHALL NOT abort peer adapters. | M | §12, §13 |
| FR-54 | Each run SHALL write a **versioned daily snapshot**; each failed source SHALL be flagged data-unavailable per source. | M | §12 |
| FR-55 | The system SHALL expose a `/pipeline/status` endpoint reporting last run date, per-source status, and overall health. | M | §11, §16 |
| FR-56 | The pipeline SHALL be idempotent for a given run_date (re-run overwrites/replaces that day's snapshot safely). | S | §13 |

## K. Dashboard & API Contract (FR-57…FR-60)
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-57 | The dashboard read SHALL always return **all four** module keys, each with a status (`ok` / `unavailable`). | M | §11 |
| FR-58 | A single failed module SHALL return **HTTP 200** with that module flagged; **HTTP 503** SHALL be reserved for total API/DB outage. | M | §11 |
| FR-59 | The dashboard SHALL present the call, target (Bear/Base/Bull), confidence, conflict flag, and per-lens breakdown together. | M | §2, §6, §11 |
| FR-60 | The API SHALL be documented by FastAPI auto-generated OpenAPI schema. | M | §11, §21 |

## L. Compliance & Disclaimer (FR-38, FR-39)
| ID | Requirement | Priority | Deck ref |
|----|-------------|----------|----------|
| FR-38 | The app SHALL present a "past ≠ future" / methodology caveat wherever backtest or projected figures appear. | M | §14 R-03 |
| **FR-39** | The app SHALL display a **persistent, non-hideable in-app disclaimer** on all recommendation surfaces (delivered via `disclaimer` payload field + `X-Disclaimer` response header per contract). **Canonical v1 wording (A8-provided 2026-07-08, for A4 to bind):** *"For personal decision-support and educational use only. Not personalized investment advice, and not a solicitation or recommendation to buy or sell any security. Not provided by a registered investment adviser (US Investment Advisers Act) / 證券投資顧問事業 (Taiwan). Signals, scores and target prices are model outputs; past performance and backtests are hypothetical and do not guarantee future results. You are solely responsible for your own decisions — consult a licensed adviser."* Attorney sign-off required before any distribution/monetization/multi-user step (A8, REG-01/R-07). | M | §7 (FR-39) |

---
### Reserved / cross-references
- NFRs are catalogued in `03_NFR.md` (NFR-01 batch SLA, NFR-02 <5s dashboard, NFR-05 encryption-at-rest preserved from deck).
- Data-source & regulatory obligations, risk register R-01…R-05: `04_regulatory_data_register.md`.
- FR numbering intentionally reserves gaps (e.g. within ranges) for review-round additions without renumbering.
