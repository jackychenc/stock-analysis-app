# UX ↔ API Field Binding (delta) — bound to contract v1.2.2
**A4 UX/UI · 2026-07-08 · task #4** · Authoritative contract: `openapi.yaml` / `domain-contract.md` (A3 v1.2.2, G1 signed off).
This is the **field-name delta** — no redesign; it binds the v0.3 component/handoff spec to the frozen field names so @SDLCSoftwareDeveloperAgent's UI and @SDLCQATestEngineerAgent's state assertions reference identical keys.
Canonical sample values = A6 `GOLDEN_WORKED_EXAMPLE.md` States A/B/C/D. Precision (v1.2.2): `composite_signal` persists **2dp** round-half-up; `per_module_breakdown[].weight_effective` displays **4dp** (e.g. 0.4286/0.3571/0.2143); `target_price` **4dp**.

## Dashboard read — `GET /stocks/{symbol}` → `Dashboard`
| Design element | API field | Notes |
|---|---|---|
| Header ticker | `ticker` | |
| "as of" date | `rec_date` (date) | freshness detail from `/pipeline/status` |
| Hero (S2) | `recommendation` (Recommendation) | see below |
| Lens summary cards (S2) | `modules.{technical\|fundamental\|chip\|news}` = `ModuleSummary{status, signal_score, headline_metric}` | **always all 4 keys**; `headline_metric` = one-line metric on the card |
| "Related names" entry (→S7) | `supply_chain_available` (bool) | show the discovery affordance only when true |

## Recommendation object — the hero (S2)
| Design element | API field | Binding rule |
|---|---|---|
| Call label (5-pt) | `composite_call` ∈ [STRONG_SELL, SELL, HOLD, BUY, STRONG_BUY, **SUPPRESSED**] | **Suppressed state ⇔ `composite_call === "SUPPRESSED"`** (bind here, NOT `status`) |
| Composite gauge + score | `composite_signal` (number, nullable −2..+2) | **null iff SUPPRESSED** → dim gauge, no score |
| Target band (Bear/Base/Bull) | `target_price.{bear,base,bull}` (object, nullable) | null when suppressed; hide band |
| Confidence badge | `confidence_level` [HIGH,MEDIUM,LOW] + `confidence_pct` (both nullable) | **now show real %** (FR-28 pinned). null when suppressed |
| Reduced-Confidence banner | `reduced_confidence` (bool) | client renders banner when true — do **not** compute |
| Conflicting-Signals chip | `conflict_flag` (bool) | server computes spread>2.0; client **reads the bool** (no client-side spread math) |
| Horizon label | `horizon_months` [3,6,12] | |
| Data-completeness bar | `data_completeness` (0..1) | show when <1 |
| Version footnote | `methodology_version` (string) | also on Backtest |
| Lens breakdown cards ×4 | `per_module_breakdown[]` = `PerModuleBreakdown{module, signal_score, weight_assigned, weight_effective, status}` | `minItems:1` — no call ships without reasoning |
| "Analysis Only" banner text | `suppressed_reason` (string, nullable) | "Analysis Only — Insufficient Data" |
| Disclaimer bar (FR-39) | `disclaimer` (string, has default) | **bind to the field, don't hardcode**; render on every recommendation surface |

### State derivation (canonical, from the fields above)
- **Normal** — `composite_call` ∈ 5-pt calls, `reduced_confidence=false`.
- **Reduced Confidence** — `reduced_confidence=true` (1 module down); call still issued; `data_completeness=0.75`.
- **Analysis Only / Suppressed** — `composite_call==="SUPPRESSED"`; `composite_signal`/`target_price`/`confidence_*` null; `suppressed_reason` set; `data_completeness≤0.5`.
- **Conflict** — `conflict_flag=true` (independent of the above).

## Lens details S3–S6 (all extend `ModuleDetail{module,status,signal_score,as_of}`)
- **S3 Technical** `TechnicalDetail` — `latest{ma20,ma60,rsi14,macd,macd_signal,macd_hist}` + `series[]{date,open,high,low,close,volume,ma20,ma60,rsi14,macd}` → PriceChart (TradingView Lightweight).
- **S4 Fundamental** `FundamentalDetail` — `pe,pb,ev_ebitda,revenue,eps,gross_margin,op_margin,net_margin` + `comparables[]{ticker,pe,pb}` → FundamentalTable. All nullable → per-metric "—" partial state.
- **S5 Chip** `ChipDetail` — `market` [TW,US]; **TW** `tw_series[]{trade_date,foreign_net,investment_trust_net,dealer_net,margin_balance,block_trade_volume}`; **US** `us_positions[]{quarter,filer_name,shares,market_value}` + `positioning_label` ("Quarterly positioning (13F, delayed)", R-04) → market-branched ChipPanel.
- **S6 News** `NewsDetail` — `aggregate_sentiment` (−1..1) + `items[]{published_at,headline,url,source_name,sentiment}` → NewsSentimentList.

## Other surfaces
- **S11 Pipeline / Freshness** `GET /pipeline/status` → `sources[].status` [ok,unavailable,running,error] → FreshnessChip + Pipeline screen.
- **S10 Settings** `GET/PUT /config` → `{module_weights, horizon_months[3,6,12]}` → WeightsEditor (sum=100% validation), horizon selector.
- **S9 Decision log** `POST` decision → `decision` [followed,ignored,partial] (+ transaction_price optional, notes) → DecisionLogForm.
- **S8 Backtest** → `{window_months[3,6,12], methodology_version, …}`.
- **Errors** `code` ∈ [SECTOR_NOT_COVERED, UNAUTHORIZED, VALIDATION_ERROR, TOTAL_OUTAGE]: **SECTOR_NOT_COVERED** → calm empty-state (S1); **TOTAL_OUTAGE** → 503 full-screen; single module fail → **200** with `modules.<m>.status="unavailable"` (never a full error).

## Net deltas vs v0.3 spec (design change = none; naming/behaviour only)
1. Suppressed binds to `composite_call==="SUPPRESSED"` (+ `suppressed_reason`), **not** a `status` field.
2. `reduced_confidence` & `conflict_flag` are **server booleans** — client renders, never computes spread/renormalisation client-side.
3. `confidence_pct` exists ⇒ hi-fi shows a real % (upgrades the interim band-only label).
4. New affordances now bindable: `headline_metric` (per lens card), `supply_chain_available` (gates S7 entry), `disclaimer` (bind, don't hardcode).
5. **Target FR-27 honesty edge** (A1): when the target's fundamental leg is unusable (fundamentals down, or `trailing_EPS≤0`, or `peer_median_PE` missing) → base falls back to `latest_close` **with Reduced-Confidence framing**. TargetPriceBand shows a "technical-only estimate" note in that case — never a silent thinner blend.

Sample values for all states = A6 `GOLDEN_WORKED_EXAMPLE.md` (canonical). Redlines (hi-fi) will annotate these exact field names on each component.
