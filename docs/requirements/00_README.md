# Requirements Baseline v0.2 — Stock Investment Analysis App
**Owner:** A2 @SDLCRequirementsBAAgent · **Date:** 2026-07-08 · **Task:** #2 (Lead A1 PM / Support A2 BA)
**Source of truth:** `stock_app_system_design_20260707_163810.pptx` v1.0 (17 slides) + A1 Product Baseline v1.1 + A3 Contract Freeze v1.2.

## Changelog v0.2.1 → v0.2.2
- **FR-28** whole-Hold-band confidence rule **A1-ratified** (contract v1.2.2 §6): Hold composite → agreement = % survivors with |signal|≤0.75; sign-match only for directional calls. Added US-3 "Hold-from-conflict → LOW confidence" scenario (QA State D). Removes the near-zero discontinuity.
- **FR-36** `weight_effective` precision (A3 v1.2.2 §2/§9): full-precision compute, 4dp store/display (0.4286/0.3571/0.2143); composite 2dp round-half-up; target 4dp.

## Changelog v0.2 → v0.2.1
- **FR-27** folded A3 contract **v1.2.1** negative-EPS/invalid-PE guard (`target_fundamental_usable = fundamental-ok AND trailing_EPS>0 AND peer_median_PE≠null`; else technical-only `latest_close` + Reduced Confidence, even if fundamental scoring module is ok). Added matching Gherkin scenario (US-3) for the negative-EPS / null-PE golden fixture. Requirements now in lockstep with the frozen contract v1.2.1.

## Changelog v0.1 → v0.2
- Folded A1's OQ rulings: OQ-1 (benchmark/>2pp/Hold-excluded), OQ-2 (6M/±15%), OQ-3 (confidence recomputed from surviving-module agreement; label independent of %).
- **FR-26** 5-point band edges locked & canonical (±1.5, boundary→milder call) — caught/fixed an asymmetry in the original ruling.
- **FR-28** confidence agreement formula = A1 authoritative (sign-match; 0-signal agrees only if Hold; composite=0 → \|signal\|≤0.75). Encoded by A3 (domain-contract v1.2 §6).
- **FR-29** conflict spread = max−min over surviving signals, strict >2.0.
- **FR-27** base-target formula resolved (A3 OD-2, v1.2 §8: 0.5·peerPE·EPS + 0.5·close; honest degradation).
- **FR-35** SUPPRESSED state explicit (no score/call/target; `suppressed_reason`; DB CHECK) — matches A3 `ck_rec_suppressed_shape`.
- **NFR-06** localhost `.env`/secret-file hygiene (Key Vault deferred to prod); **NFR-10b** local Docker Compose target.
- **RTM Design column filled** from A3 Contract Freeze v1.1/v1.2 (endpoints, tables, constraints, domain-contract sections).

## Package contents
| File | What it is |
|------|-----------|
| `00_README.md` | This index |
| `01_BRD.md` | Business Requirements: vision, persona, goals/metrics, scope & out-of-scope, stakeholders, assumptions, open questions |
| `02_SRS_FR.md` | 60 Functional Requirements (FR-01…FR-60), MoSCoW-prioritised, deck-referenced |
| `03_NFR.md` | 20 Non-Functional Requirements (NFR-01…NFR-19 + NFR-10b) with measurable targets |
| `04_regulatory_data_register.md` | Compliance obligations (REG-01…06), data-source licensing table, risk register (R-01…R-07) |
| `05_user_stories_gherkin.md` | 12 user stories with Gherkin AC; **every scenario tagged `@FR-NN`** for RTM auto-link (A6) |
| `06_RTM.md` | Requirements Traceability Matrix: BusinessGoal → Req → Deck → Design(A3) → Code(A5) → Test(A6) |

## Key framing decisions (baseline)
1. **Decision-support tool, NOT a brokerage/trading platform.** No execution/custody/funds/KYC-AML. Regulatory surface is narrow (disclaimer FR-39, data ToS, PII NFR-05, recommendation record-keeping).
2. **Batch-only, snapshot reads.** Fresh daily by 07:00 TW (NFR-01); dashboard <5s (NFR-02).
3. **Never-silent degradation** (FR-34/35/37) and **honest backtest** (FR-40..43) are the product differentiators → A1/A6 mark these BLOCK-level at G2.
4. **Deployment:** local host / Docker Compose for testing (per @jackychenc); Azure PaaS §23 = deferred production target.

## Handoffs
- **A3 (task #3 contract freeze):** please resolve — FR-26 band-edge semantics (inclusive/exclusive at −2.0/−0.75/+0.75/+2.0 and Strong/regular split), `per_module_breakdown` JSONB shape, renormalisation/suppression contract, OpenAPI (§21) + schema DDL (§20). Fill Design column in `06_RTM.md`.
- **A6 (task #5):** Gherkin `@FR` tags reconcile 1:1 into `RTM_TEST_LEG`. Coverage gaps → flag back to A2.
- **A8 (pending):** own REG register, legal-review FR-39 wording, confirm R-05, define NFR-19 retention.

## Status: v0.1 posted for team review → task #2 to **in_review** pending @jackychenc / A1 acceptance.
