# Task #20 — On-demand Ticker Registration · UX state spec
**A4 UX/UI · 2026-07-09** · Design owner: @SDLCUXUIDesignerAgent. Binds to **ADR-009 v1.2.10** (A3 status enum + polling) + **requirements v0.2.15** (A2: FR-61 pool cap, FR-62 canonical enum `queued|running|ready|partial|failed` + sanitized failure/400, NFR-22 loopback job infra, US-14) + A8 security deltas. Enum + coverage/stale/as_of read-time flags are identical across contract ↔ requirements ↔ this UI spec. Additive to the task #14 Dashboard — reuses all existing components; adds a search-result affordance + a pending screen + a stale/Refresh affordance.

## Contract this binds to (ADR-009)
- **Job status** (`GET /analyze/{run_id}.status`): `queued | running(+phase∈{fetching,scoring}) | ready | partial | failed`. Terminal = {ready, partial, failed}.
- **Snapshot flags** (`GET /dashboard/{ticker}`, read-time): `coverage∈{covered,not_covered}`, `stale:boolean`, `as_of:timestamp`.
- **Polling:** `POST /analyze {ticker, force?}` → SYMBOL_RE fail-fast **400** at ingress · fresh & !force ⇒ **200** `{ready, stale:false, as_of}` · else **202** `{run_id, queued, poll_after_ms}`. Poll `GET /analyze/{run_id}` every `poll_after_ms` until terminal; on ready|partial → read `GET /dashboard/{ticker}`. **Refresh** = `POST /analyze {force:true}` (honors cooldown; never silent auto-refetch).

## The 6 UI states (+ 2 guards) — state → API → UI

| # | State | Triggered by | UI treatment |
|---|-------|-------------|-------------|
| 1 | **Search-miss** | dashboard `coverage:not_covered` | Card: **"[TICKER] isn't analyzed yet."** + primary CTA **"Analyze [TICKER]"** + subtext *"First analysis takes ~1–2 min, then it updates daily."* Replaces the old dead-end "not covered" banner. |
| — | *Invalid symbol* (guard) | `POST /analyze` → 400 (SYMBOL_RE) | Inline error under the search box: *"'xyz' isn't a valid ticker symbol."* No CTA, no fetch (fail-closed at ingress). |
| — | *Coverage cap* (guard) | add would exceed `max_coverage_pool_size` (FR-61, enforced in #20) | **Reject mode:** *"You're tracking the max N tickers. Remove one (Settings) to add another."* — block the add. **LRU-evict mode:** allow the add but **surface it, don't silently drop** — *"Added [TICKER]. [OLDEST] dropped to stay within N tracked tickers."* (Design supports whichever the config sets; management UI is #14.) |
| 2 | **Pending / computing** | `POST /analyze` → 202 → poll `queued`/`running` | Full-width progress card: indeterminate spinner + **"Analyzing [TICKER]…"** + phase substep: `queued`→"Queued…" · `running.phase=fetching`→"Fetching price, fundamentals, chip & news…" · `running.phase=scoring`→"Scoring the five lenses…". Poll every `poll_after_ms`. No fake % bar. FR-39 bar stays. |
| 3 | **Ready** | terminal `ready` → `GET /dashboard/{ticker}` | Transition into the normal **Dashboard** (all existing components) with the **"as of [as_of]"** freshness chip. Instant on every later visit (snapshot). |
| 4 | **Partial** | terminal `partial` → dashboard | Normal Dashboard + **Reduced Confidence** amber banner (≥1 lens `unavailable`, renormalised) + completeness bar — **reuses shipped states**. (≥2 scoring lenses down ⇒ dashboard renders SUPPRESSED "Analysis Only" per existing rules.) Chip single-quarter-13F → "unavailable" w/ `positioning_label` "13F baseline captured — direction available next quarter" (A3 ruling). |
| 5 | **Failure** | terminal `failed` + FR-62 sanitized reason category | Error card: **"Couldn't analyze [TICKER] right now."** + one **user-facing line per sanitized category** (never internals/PII/stacktrace): `source_unavailable`→"A data source is temporarily unavailable." · `fetch_failed`→"We couldn't retrieve data for this ticker." · `timeout`→"Analysis took too long — try again." + **"Try again"** (re-POST /analyze). Never a fabricated result. |
| 6 | **Stale / Refresh** | dashboard `stale:true` | "as of [as_of]" chip → **amber** + a **"Refresh"** button (*"Re-run analysis"*). Refresh = `POST /analyze {force:true}` (honors cooldown) → returns to **state 2**. **No silent auto-refresh** — user always initiates. |

## Flow
Search a ticker → covered? → **Dashboard (3/4/6)**. Not covered → **Search-miss (1)** → Analyze → **Pending (2)** → terminal → **Ready(3)/Partial(4)/Failure(5)**. Any covered ticker whose snapshot is stale → **Refresh (6)**.

## Design principles carried over
- **Honesty:** never a fabricated signal — pending shows real phase, partial reuses `unavailable`/Reduced-Confidence, failure is honest, stale is explicit (no silent refresh).
- **Additive:** ready/partial reuse the existing Dashboard + safety-UX matrix; only states 1/2/5/6 are new surfaces.
- **Snapshot architecture unchanged:** covered tickers stay instant (<5s); only the first-fetch path is new.
- **Accessibility:** pending spinner has an SR live-region ("Analyzing… fetching data"); Refresh/Analyze are ≥44px targets; states announce on change.
- **Security (A8):** SYMBOL_RE ingress 400 → invalid-symbol guard; coverage cap + format-validate enforced here; generic failure copy (no internal detail).

## Deliverables
- This spec = the UI acceptance criteria (Cindy-approved 6 states).
- Wireframe: `wireframes/task20-states.html/png` (states 1/2/5/6 — the new surfaces; 3/4 = existing Dashboard).
- Open to A3: if the pending state should show per-lens progress, it needs a per-lens status during `running` (currently only global `phase`) — otherwise per-lens appears at ready/partial, which is fine.
