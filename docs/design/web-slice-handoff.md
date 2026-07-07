# Web-slice (task #17) — A4 design handoff
**A4 UX/UI · 2026-07-08** · For @SDLCSoftwareDeveloperAgent's minimal clickable web slice: login + Dashboard 3 states + FR-39 bar. Light-first. Binds to contract **v1.2.4** + A6 `golden_fixtures.json`.
Companion artifacts: hi-fi redlines `hifi/hifi-dashboard-redlined.png`, field map `API-field-binding-v1.2.2.md` (field NAMES current on v1.2.4), states `wireframes/dashboard-states.png`, disclaimer `disclaimer-and-microcopy.md`.

## 1. Light-first design tokens (drop into Next.js global CSS / Tailwind theme)
```css
:root{
  /* surfaces */
  --bg:#eef1f5; --card:#ffffff; --ink:#0f172a; --sub:#64748b; --line:#e2e8f0; --line-2:#eef2f7;
  /* diverging signal palette −2…+2 (ALWAYS paired with icon ▲▼▬ + sign + label; never color alone) */
  --sig-ss:#c0392b;  /* −2 Strong Sell */
  --sig-s:#e06666;   /* −1 Sell */
  --sig-hold:#64748b;/*  0 Hold  */
  --sig-b:#22a565;   /* +1 Buy */
  --sig-sb:#15803d;  /* +2 Strong Buy */
  --gauge-grad:linear-gradient(90deg,#c0392b,#cbd5e1 50%,#15803d);
  /* state semantics */
  --amber:#b45309; --amber-bg:#fef3c7;   /* Reduced Confidence */
  --red:#991b1b;   --red-bg:#fdecec;     /* Analysis-Only / suppressed */
  --conf-hi-bg:#eafff2; --conf-hi-ink:#166534;
  --accent:#2563eb;
  /* shape + elevation */
  --r-card:14px; --r-hero:16px; --r-chip:9px; --r-pill:12px;
  --shadow:0 1px 3px rgba(15,23,42,.06),0 8px 24px rgba(15,23,42,.06);
  /* type scale */
  --fs-call:20px/700; --fs-score:24px/800; --fs-ticker:22px/800;
  --fs-body:14px; --fs-metric:12px; --fs-micro:10.5px;
}
/* numbers: tabular figures everywhere money/signals render */
.num{font-variant-numeric:tabular-nums;font-feature-settings:"tnum";}
```
Dark-mode later = swap the surface + line vars under a `.dark` scope; signal/state hues already AA in both. Tailwind: map the above into `theme.extend.colors` (signal.ss/s/hold/b/sb, state.amber/red) + `borderRadius`.

## 2. Signal rendering rule (accessibility, WCAG 2.1 AA)
Every lens/call signal = **color + icon + sign + number**, never color alone:
`|signal| ≥ 1.5` → double arrow ▲▲ / ▼▼ (Strong zone) · `0 < |signal| < 1.5` → single ▲ / ▼ · `0` → ▬ (Hold).
Contrast ≥ 4.5:1 text / 3:1 UI; touch/click targets ≥ 44px; disclaimer bar meets AA.

## 3. The 3 states — golden values + field bindings (2330.TW, contract v1.2.4)
Data source = `golden_fixtures.json` D1. Bind to these exact fields; render 2dp composite, 4dp weight_effective.

| element | field | Normal (GF-A) | Reduced-Conf (GF-B) | Analysis-Only (GF-C) |
|---|---|---|---|---|
| call label | `composite_call` | BUY | BUY | **SUPPRESSED** → "No recommendation" |
| composite | `composite_signal` (2dp) | +0.90 | +0.77 | null (hide gauge/score) |
| gauge tick | (signal+2)/4 | 72.5% | 69.25% | — |
| target | `target_price.{bear,base,bull}` | 688.50 / 810.00 / 931.50 | 688.50 / 810.00 / 931.50 | null (hide band) |
| confidence | `confidence_level`+`confidence_pct` | HIGH · 75% | MEDIUM · 66.67% | null |
| reduced-conf banner | `reduced_confidence` (bool) | false | **true** (amber banner) | — |
| conflict chip | `conflict_flag` (bool) | false | false | — |
| completeness | `data_completeness` | 1.00 (hide bar) | 0.75 (show) | 0.50 (show) |
| suppressed banner | `suppressed_reason` | — | — | "Analysis Only — Insufficient Data" |
| lens cards ×4 | `per_module_breakdown[]` {signal_score, weight_assigned, weight_effective, status} | all ok, eff 30/30/25/15 | Technical `unavailable` eff 0; F/C/N eff 0.4286/0.3571/0.2143 | Technical+Chip `unavailable`; F/N shown-not-scored |
| disclaimer bar | `disclaimer` + `X-Disclaimer` | present every state | present | present |
| version | `disclaimer_version` | "fr39-v1" | " | " |

**State rules (client reads booleans, never computes):** suppressed ⇔ `composite_call==="SUPPRESSED"`; reduced-conf banner ⇔ `reduced_confidence`; conflict chip ⇔ `conflict_flag`. Single failed module = 200 with `status:"unavailable"` badge; 503 only total outage.

**Pre-engine EMPTY-STATE (handle gracefully — per A5):** a covered ticker with **no recommendation rows yet** returns **200** with `rec_date:null` + all 4 modules `unavailable` + disclaimer present (NOT 503 — the honest empty-state is the feature). UI: show a calm "No analysis yet — daily batch hasn't run for this ticker" card (distinct from SUPPRESSED, which is a *computed* decision to withhold). The 3 golden states above appear once fixtures/engine (task #10) seed data. SECTOR_NOT_COVERED = separate calm empty-state on search.

## 4. FR-39 disclaimer bar (persistent, non-hideable)
- Concise **client-side** line (this slice), always visible on the dashboard, WCAG AA, non-dismissible:
  *"For personal decision-support & educational use only — not investment advice; model outputs, not from a registered adviser. Full disclaimer ›"*
- "Full disclaimer ›" → shows the full canonical `recommendation.disclaimer` payload text (ASCII, from `DISCLAIMER` constant) — for the mini-slice, an inline expand or modal is fine (first-run ack + Settings→Legal are full task #14).
- FR-38 inline caveat under the target band: *"Hypothetical model output — not a price prediction."*

## 5. Login page (minimal)
Single personal account (no signup/KYC). Email/username + password → session cookie. On success → Dashboard. Error state = inline "Incorrect credentials." Keep it on the same token set.

## 6. Acceptance (matches A6 smoke)
Screens render the 3 golden states pixel-faithfully to the mockups; numbers byte-match `golden_fixtures.json`; FR-39 bar present + `X-Disclaimer` header shown; light-first tokens applied. I'll redline the running build once @SDLCDevOpsSREAgent posts the :3000 endpoint.
