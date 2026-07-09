"use client";

/** Dashboard — three golden states + honest empty-state (task #17), plus the
 * on-demand analyze flow (task #20/#22): search-miss → pending (real phases,
 * no fake %) → ready/partial (existing Dashboard) / failure, and the explicit
 * stale-chip + Refresh affordance (server-authoritative cooldown).
 * Client reads server booleans/fields only (composite_call / reduced_confidence
 * / conflict_flag); it NEVER computes spread, renormalisation or suppression.
 * Field bindings + rendering rules: A4 web-slice-handoff.md §3 +
 * task20-on-demand-registration-spec.md (6 states). */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  AnalyzeAccepted,
  AnalyzeFailureReason,
  AnalyzeJob,
  AnalyzeJobStatus,
  AnalyzePhase,
  CALL_LABELS,
  CALL_RENDER,
  Dashboard,
  FAILURE_COPY,
  fmtSigned,
  MODULE_LABELS,
  ModuleName,
  PerModuleBreakdown,
  signalColorVar,
  signalIcon,
  SYMBOL_RE,
} from "@/lib/contract";

const MODULE_ORDER: ModuleName[] = ["technical", "fundamental", "chip", "news"];

/** On-demand analyze flow (spec states 1/2/5; 3/4/6 render via DashboardView).
 * `force` is remembered so "Try again" re-POSTs the same request. */
type AnalyzeFlow =
  | { kind: "idle" }
  | { kind: "miss"; ticker: string; error: string | null }
  | { kind: "pending"; ticker: string; runId: string; pollAfterMs: number; force: boolean }
  | { kind: "failed"; ticker: string; reason: AnalyzeFailureReason | null; force: boolean };

export default function DashboardPage() {
  const router = useRouter();
  const [ticker, setTicker] = useState("GF-A");
  const [query, setQuery] = useState("GF-A");
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [flow, setFlow] = useState<AnalyzeFlow>({ kind: "idle" });
  // Current pending step, driven ONLY by server status/phase (no fake progress).
  const [job, setJob] = useState<{ status: AnalyzeJobStatus; phase: AnalyzePhase | null }>(
    { status: "queued", phase: null },
  );
  const [posting, setPosting] = useState(false);
  // Symbol-guard error under the search box (client pre-check or server 400).
  const [inlineError, setInlineError] = useState<string | null>(null);

  const load = useCallback(
    async (symbol: string) => {
      setBusy(true);
      setError(null);
      try {
        const r = await fetch(
          `/api/v1/stocks/${encodeURIComponent(symbol)}/dashboard`,
          { credentials: "include" },
        );
        if (r.status === 401) {
          router.push("/");
          return;
        }
        if (r.status === 404) {
          const body = await r.json().catch(() => null);
          setData(null);
          if (body?.detail?.code === "SECTOR_NOT_COVERED") {
            // State 1 (search-miss): not a dead-end any more — offer Analyze.
            setFlow({ kind: "miss", ticker: symbol, error: null });
          } else {
            setError("Ticker not found.");
          }
          return;
        }
        if (!r.ok) {
          setData(null);
          setError(`Service unavailable (${r.status}).`);
          return;
        }
        setData((await r.json()) as Dashboard);
        setFlow({ kind: "idle" });
      } catch {
        setData(null);
        setError("Cannot reach the API — is the local stack running?");
      } finally {
        setBusy(false);
      }
    },
    [router],
  );

  /** POST /analyze — the single entry into the analyze flow (search-miss CTA,
   * Refresh {force:true}, and failure "Try again" all land here). */
  const startAnalyze = useCallback(
    async (symbol: string, force: boolean) => {
      const t = symbol.trim().toUpperCase();
      // Instant client pre-check mirroring server SYMBOL_RE; server 400 stays
      // authoritative below (fail-closed at ingress — no fetch on a miss).
      if (!SYMBOL_RE.test(t)) {
        setInlineError(`'${symbol.trim()}' isn't a valid ticker symbol.`);
        return;
      }
      setPosting(true);
      setInlineError(null);
      try {
        const r = await fetch("/api/v1/analyze", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(force ? { ticker: t, force: true } : { ticker: t }),
          credentials: "include",
        });
        if (r.status === 401) {
          router.push("/");
          return;
        }
        if (r.status === 202) {
          // State 2 (pending): poll every poll_after_ms until terminal.
          const body = (await r.json()) as AnalyzeAccepted;
          setJob({ status: "queued", phase: null });
          setFlow({
            kind: "pending",
            ticker: t,
            runId: body.run_id,
            pollAfterMs: body.poll_after_ms,
            force,
          });
          return;
        }
        if (r.ok) {
          // 200 fresh/cooldown short-circuit — the snapshot answers; show it.
          setFlow({ kind: "idle" });
          await load(t);
          return;
        }
        const body = await r.json().catch(() => null);
        const message: string =
          body?.detail?.message ?? `Service unavailable (${r.status}).`;
        if (r.status === 400) {
          setInlineError(message); // server symbol guard is authoritative
          return;
        }
        if (r.status === 409) {
          // FR-61 coverage-pool cap: server's actionable message, verbatim.
          setFlow({ kind: "miss", ticker: t, error: message });
          return;
        }
        setFlow({ kind: "failed", ticker: t, reason: null, force });
      } catch {
        setFlow({ kind: "failed", ticker: t, reason: null, force });
      } finally {
        setPosting(false);
      }
    },
    [router, load],
  );

  // State 2 polling loop: GET /analyze/{run_id} every poll_after_ms.
  // Terminal ready|partial → re-fetch the dashboard (states 3/4 reuse it);
  // failed → state 5. The step list is driven only by status/phase.
  useEffect(() => {
    if (flow.kind !== "pending") return;
    const { ticker: t, runId, pollAfterMs, force } = flow;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const r = await fetch(`/api/v1/analyze/${encodeURIComponent(runId)}`, {
          credentials: "include",
        });
        if (cancelled) return;
        if (r.status === 401) {
          router.push("/");
          return;
        }
        if (!r.ok) {
          // 404 unknown run_id or transient server error — honest failure.
          setFlow({ kind: "failed", ticker: t, reason: null, force });
          return;
        }
        const j = (await r.json()) as AnalyzeJob;
        if (cancelled) return;
        if (j.status === "ready" || j.status === "partial") {
          setFlow({ kind: "idle" });
          void load(t);
          return;
        }
        if (j.status === "failed") {
          setFlow({ kind: "failed", ticker: t, reason: j.reason ?? null, force });
          return;
        }
        setJob({ status: j.status, phase: j.phase ?? null });
      } catch {
        if (cancelled) return; // network blip — keep polling
      }
      timer = setTimeout(poll, pollAfterMs);
    }
    timer = setTimeout(poll, pollAfterMs);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [flow, router, load]);

  // Review/deep-link support: /dashboard?ticker=GF-B drives the state directly.
  useEffect(() => {
    const fromUrl = new URLSearchParams(window.location.search).get("ticker");
    if (fromUrl) {
      const symbol = fromUrl.trim().toUpperCase();
      setTicker(symbol);
      setQuery(symbol);
    }
  }, []);

  useEffect(() => {
    void load(query);
  }, [query, load]);

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-extrabold">Stock Investment Analysis</h1>
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            const t = ticker.trim().toUpperCase();
            // Invalid-symbol guard: instant inline error, no fetch
            // (fail-closed at ingress; server 400 remains authoritative).
            if (!SYMBOL_RE.test(t)) {
              setInlineError(`'${ticker.trim()}' isn't a valid ticker symbol.`);
              return;
            }
            setInlineError(null);
            setFlow({ kind: "idle" });
            setQuery(t);
          }}
        >
          <input
            aria-label="Ticker"
            className="w-44 rounded-lg border bg-white px-3 py-2 text-sm outline-none"
            style={{ borderColor: "var(--line)" }}
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            placeholder="e.g. 2330.TW, AAPL"
          />
          <button
            type="submit"
            disabled={busy}
            aria-disabled={busy}
            className="min-h-11 rounded-xl px-4 text-sm font-semibold text-white disabled:opacity-60"
            style={{ background: "var(--accent)" }}
          >
            {busy ? "Loading…" : "Analyze"}
          </button>
        </form>
      </header>

      {inlineError && (
        <p
          role="alert"
          data-testid="symbol-error"
          className="rounded-lg px-3 py-2 text-sm"
          style={{ background: "var(--red-bg)", color: "var(--red)" }}
        >
          {inlineError}
        </p>
      )}

      {flow.kind === "miss" && (
        <SearchMissCard
          ticker={flow.ticker}
          error={flow.error}
          busy={posting}
          onAnalyze={() => void startAnalyze(flow.ticker, false)}
        />
      )}
      {flow.kind === "pending" && (
        <PendingCard ticker={flow.ticker} status={job.status} phase={job.phase} />
      )}
      {flow.kind === "failed" && (
        <FailureCard
          ticker={flow.ticker}
          reason={flow.reason}
          busy={posting}
          onRetry={() => void startAnalyze(flow.ticker, flow.force)}
        />
      )}

      {flow.kind === "idle" && error && (
        <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
          {error}
        </div>
      )}

      {flow.kind === "idle" && data && (
        <DashboardView
          data={data}
          refreshing={posting}
          onRefresh={() => void startAnalyze(data.ticker, true)}
        />
      )}
    </div>
  );
}

function DashboardView({
  data, refreshing, onRefresh,
}: {
  data: Dashboard; refreshing: boolean; onRefresh: () => void;
}) {
  const rec = data.recommendation;
  // State 6: stale is derivable read-time — snapshot as_of < today (UTC,
  // matching the server's clock). Explicit chip + user-initiated Refresh only.
  const stale =
    data.rec_date !== null &&
    data.rec_date < new Date().toISOString().slice(0, 10);

  // Honest empty-state: pre-engine, no snapshot yet (distinct from SUPPRESSED).
  if (data.rec_date === null || rec === null) {
    return (
      <section className="card flex flex-col items-center gap-2 p-10 text-center">
        <span className="text-2xl font-extrabold num">{data.ticker}</span>
        <p className="text-sm font-semibold">No analysis yet</p>
        <p className="text-sm" style={{ color: "var(--sub)" }}>
          The daily batch hasn&apos;t produced a snapshot for this ticker.
          Check back after the next run.
        </p>
      </section>
    );
  }

  const suppressed = rec.composite_call === "SUPPRESSED";

  return (
    <div className="flex flex-col gap-4">
      {/* status banners — server booleans only */}
      {suppressed && (
        <Banner bg="var(--red-bg)" ink="var(--red)" testid="banner-suppressed">
          <strong>{rec.suppressed_reason ?? "Analysis Only — Insufficient Data"}</strong>
          <span>
            &nbsp;— {4 - rec.per_module_breakdown.filter((b) => b.status === "ok").length}{" "}
            of 4 lenses unavailable, so no call, score or target is issued.
          </span>
        </Banner>
      )}
      {rec.reduced_confidence && !suppressed && (
        <Banner bg="var(--amber-bg)" ink="var(--amber)" testid="banner-reduced">
          <strong>Reduced Confidence</strong>
          <span>
            &nbsp;— a lens is unavailable; remaining weights were renormalised.
          </span>
        </Banner>
      )}

      {/* hero */}
      <section className="card p-6" style={{ borderRadius: "var(--r-hero)" }}>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="text-2xl font-extrabold num">{data.ticker}</div>
            <div className="text-xs" style={{ color: "var(--sub)" }}>
              Snapshot {data.rec_date} · horizon {rec.horizon_months}M ·{" "}
              {rec.methodology_version}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            {data.rec_date && (
              <span
                data-testid="chip-asof"
                className="rounded-lg px-2.5 py-1 text-xs font-semibold num"
                style={
                  stale
                    ? { background: "var(--amber-bg)", color: "var(--amber)" }
                    : { background: "var(--line-2)", color: "var(--sub)" }
                }
              >
                as of {data.rec_date}
              </span>
            )}
            {stale && (
              <RefreshControl
                nextRefreshAt={data.next_refresh_at}
                busy={refreshing}
                onRefresh={onRefresh}
              />
            )}
            {rec.conflict_flag && (
              <span
                data-testid="chip-conflict"
                className="rounded-lg px-2.5 py-1 text-xs font-semibold"
                style={{ background: "var(--amber-bg)", color: "var(--amber)" }}
              >
                ⚠ Conflicting Signals
              </span>
            )}
            {rec.confidence_level && (
              <span
                className="rounded-lg px-2.5 py-1 text-xs font-semibold num"
                style={confidenceBadgeStyle(rec.confidence_level)}
              >
                Confidence: {rec.confidence_level}
                {rec.confidence_pct !== null && ` ${trimPct(rec.confidence_pct)}%`}
              </span>
            )}
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-6">
          <div>
            {/* F1: glyph/color from the CALL, not the signal's sign */}
            <div
              className="text-xl font-bold"
              style={{ color: CALL_RENDER[rec.composite_call].color }}
              data-testid="call-label"
            >
              {CALL_RENDER[rec.composite_call].icon && (
                <span aria-hidden>{CALL_RENDER[rec.composite_call].icon}&nbsp;</span>
              )}
              {CALL_LABELS[rec.composite_call]}
            </div>
            {rec.composite_signal !== null && (
              <div className="num text-2xl font-extrabold" data-testid="composite-score">
                {fmtSigned(rec.composite_signal)}
              </div>
            )}
          </div>

          {/* gauge only when a score exists */}
          {rec.composite_signal !== null && (
            <div className="min-w-56 flex-1">
              <div
                className="relative h-2.5 rounded-full"
                style={{ background: "var(--gauge-grad)" }}
                role="img"
                aria-label={`Composite ${fmtSigned(rec.composite_signal)} on a −2 to +2 scale`}
              >
                <div
                  className="absolute top-1/2 h-4 w-1.5 -translate-y-1/2 rounded bg-white shadow"
                  style={{
                    left: `${(((rec.composite_signal + 2) / 4) * 100).toFixed(2)}%`,
                    border: "1px solid var(--ink)",
                  }}
                />
              </div>
              <div className="mt-1 flex justify-between text-[10.5px]" style={{ color: "var(--sub)" }}>
                <span>−2 Strong Sell</span>
                <span>0 Hold</span>
                <span>+2 Strong Buy</span>
              </div>
            </div>
          )}
        </div>

        {/* target band (hidden when suppressed / null) */}
        {rec.target_price?.base != null && (
          <div className="mt-5 border-t pt-4" style={{ borderColor: "var(--line-2)" }}>
            <div className="flex flex-wrap items-end gap-6" data-testid="target-band">
              <TargetCell label="Bear −15%" value={rec.target_price.bear} muted />
              <TargetCell label="Base target" value={rec.target_price.base} />
              <TargetCell label="Bull +15%" value={rec.target_price.bull} muted />
            </div>
            {/* FR-38: caveat adjacent to every target figure */}
            <p className="mt-2 text-[10.5px]" style={{ color: "var(--sub)" }}>
              Hypothetical model output — not a price prediction.
            </p>
          </div>
        )}

        {/* data completeness (shown when < 1) */}
        {rec.data_completeness < 1 && (
          <div className="mt-4 flex items-center gap-2 text-xs" style={{ color: "var(--sub)" }}>
            <span>Data completeness</span>
            <div className="h-1.5 w-36 overflow-hidden rounded-full" style={{ background: "var(--line)" }}>
              <div
                className="h-full rounded-full"
                style={{ width: `${rec.data_completeness * 100}%`, background: "var(--accent)" }}
              />
            </div>
            <span className="num">{rec.data_completeness.toFixed(2)}</span>
          </div>
        )}
      </section>

      {/* lens cards */}
      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {MODULE_ORDER.map((m) => (
          <LensCard
            key={m}
            entry={rec.per_module_breakdown.find((b) => b.module === m)}
            module={m}
          />
        ))}
      </section>
    </div>
  );
}

function Banner({
  bg, ink, children, testid,
}: {
  bg: string; ink: string; children: React.ReactNode; testid: string;
}) {
  return (
    <div
      role="status"
      data-testid={testid}
      className="rounded-xl px-4 py-3 text-sm"
      style={{ background: bg, color: ink }}
    >
      {children}
    </div>
  );
}

function TargetCell({
  label, value, muted = false,
}: {
  label: string; value: number | null; muted?: boolean;
}) {
  return (
    <div>
      <div className="text-[10.5px] uppercase tracking-wide" style={{ color: "var(--sub)" }}>
        {label}
      </div>
      <div
        className={`num font-bold ${muted ? "text-base" : "text-xl"}`}
        style={{ color: muted ? "var(--sub)" : "var(--ink)" }}
      >
        {value != null ? value.toFixed(2) : "—"}
      </div>
    </div>
  );
}

function LensCard({
  entry, module,
}: {
  entry: PerModuleBreakdown | undefined; module: ModuleName;
}) {
  const unavailable = !entry || entry.status === "unavailable";
  const renormalised =
    entry && entry.status === "ok" &&
    Math.abs(entry.weight_effective - entry.weight_assigned) > 1e-9;
  return (
    <div className="card p-4" data-testid={`lens-${module}`}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold">{MODULE_LABELS[module]}</span>
        {unavailable && (
          <span
            className="rounded px-1.5 py-0.5 text-[10.5px] font-semibold"
            style={{ background: "var(--red-bg)", color: "var(--red)" }}
          >
            unavailable
          </span>
        )}
      </div>
      <div className="mt-2 num text-lg font-extrabold"
        style={{ color: unavailable ? "var(--sub)" : signalColorVar(entry!.signal_score) }}>
        {unavailable || entry!.signal_score === null ? (
          "—"
        ) : (
          <>
            <span aria-hidden>{signalIcon(entry!.signal_score)}&nbsp;</span>
            {fmtSigned(entry!.signal_score, 1)}
          </>
        )}
      </div>
      {entry && (
        <div className="mt-2 text-[10.5px]" style={{ color: "var(--sub)" }}>
          weight {entry.weight_assigned.toFixed(2)}
          {/* F3: show "→ effective" only for a genuine renormalisation
              (never when effective equals assigned or is a 0 data anomaly) */}
          {renormalised && entry.weight_effective > 0 && (
            <>
              {" "}→ <span className="num font-semibold">{entry.weight_effective.toFixed(4)}</span> effective
            </>
          )}
          {unavailable && " → 0 effective"}
        </div>
      )}
    </div>
  );
}

function trimPct(pct: number): string {
  return Number.isInteger(pct) ? String(pct) : pct.toFixed(2);
}

/** A4 polish: LOW must not reuse the positive green style. */
function confidenceBadgeStyle(level: "HIGH" | "MEDIUM" | "LOW"): React.CSSProperties {
  if (level === "HIGH") return { background: "var(--conf-hi-bg)", color: "var(--conf-hi-ink)" };
  if (level === "MEDIUM") return { background: "var(--amber-bg)", color: "var(--amber)" };
  return { background: "var(--line-2)", color: "var(--sub)" };
}

/* ---- On-demand analyze states (task #20 spec states 1/2/5/6) ------------- */

/** State 1 — search-miss: not-covered is an invitation, not a dead-end.
 * The 409 pool-cap message (server, verbatim) also lands here. */
function SearchMissCard({
  ticker, error, busy, onAnalyze,
}: {
  ticker: string; error: string | null; busy: boolean; onAnalyze: () => void;
}) {
  return (
    <section className="card flex flex-col items-center gap-2 p-10 text-center"
      data-testid="search-miss">
      <span className="text-2xl font-extrabold num">{ticker}</span>
      <p className="text-sm font-semibold">{ticker} isn&apos;t analyzed yet.</p>
      <p className="text-sm" style={{ color: "var(--sub)" }}>
        First analysis takes ~1–2 min, then it updates daily.
      </p>
      <button
        type="button"
        onClick={onAnalyze}
        disabled={busy}
        aria-disabled={busy}
        className="mt-2 min-h-11 rounded-xl px-5 text-sm font-semibold text-white disabled:opacity-60"
        style={{ background: "var(--accent)" }}
        data-testid="btn-analyze"
      >
        {busy ? "Starting…" : `Analyze ${ticker}`}
      </button>
      {error && (
        <p role="alert" className="mt-2 rounded-lg px-3 py-2 text-sm"
          style={{ background: "var(--red-bg)", color: "var(--red)" }}>
          {error}
        </p>
      )}
    </section>
  );
}

/** State 2 — pending. Step list driven ONLY by server status/phase:
 * queued → 1 · running+fetching → 2 · running+scoring → 3. No fake % bar. */
const PENDING_STEPS: { label: string; detail: string }[] = [
  { label: "Queued", detail: "Queued…" },
  { label: "Fetching data", detail: "Fetching price, fundamentals, chip & news…" },
  { label: "Scoring", detail: "Scoring the five lenses…" },
];

function PendingCard({
  ticker, status, phase,
}: {
  ticker: string; status: AnalyzeJobStatus; phase: AnalyzePhase | null;
}) {
  const active = status === "queued" ? 0 : phase === "scoring" ? 2 : 1;
  return (
    <section className="card flex flex-col items-center gap-4 p-10 text-center"
      data-testid="analyze-pending">
      <span
        aria-hidden
        className="h-8 w-8 animate-spin rounded-full border-2"
        style={{ borderColor: "var(--accent)", borderTopColor: "transparent" }}
      />
      <div>
        <p className="text-base font-semibold">Analyzing {ticker}…</p>
        <p className="mt-1 text-sm" style={{ color: "var(--sub)" }}>
          Usually takes ~1–2 min.
        </p>
      </div>
      <ol aria-live="polite" className="flex flex-col items-start gap-1.5 text-sm">
        {PENDING_STEPS.map((s, i) => (
          <li
            key={s.label}
            className={i === active ? "font-semibold" : ""}
            style={{ color: i === active ? "var(--ink)" : "var(--sub)" }}
          >
            <span aria-hidden>{i < active ? "✓ " : i === active ? "▸ " : "○ "}</span>
            {i === active ? s.detail : s.label}
          </li>
        ))}
      </ol>
    </section>
  );
}

/** State 5 — failure: honest, one sanitized line per FR-62 category; never a
 * fabricated result, never raw internals. */
function FailureCard({
  ticker, reason, busy, onRetry,
}: {
  ticker: string;
  reason: AnalyzeFailureReason | null;
  busy: boolean;
  onRetry: () => void;
}) {
  return (
    <section className="card flex flex-col items-center gap-2 p-10 text-center"
      data-testid="analyze-failed">
      <p className="text-base font-semibold" style={{ color: "var(--red)" }}>
        Couldn&apos;t analyze {ticker} right now.
      </p>
      <p role="alert" className="text-sm" style={{ color: "var(--sub)" }}>
        {reason ? FAILURE_COPY[reason] : "Something went wrong — try again."}
      </p>
      <button
        type="button"
        onClick={onRetry}
        disabled={busy}
        aria-disabled={busy}
        className="mt-2 min-h-11 rounded-xl px-5 text-sm font-semibold text-white disabled:opacity-60"
        style={{ background: "var(--accent)" }}
        data-testid="btn-retry"
      >
        {busy ? "Starting…" : "Try again"}
      </button>
    </section>
  );
}

/** State 6 — Refresh. Server-authoritative cooldown: while next_refresh_at is
 * in the future the button is disabled with a live ~30s-refreshed countdown —
 * never a silent no-op click. Refresh = POST /analyze {force:true} → state 2. */
function RefreshControl({
  nextRefreshAt, busy, onRefresh,
}: {
  nextRefreshAt: string | null; busy: boolean; onRefresh: () => void;
}) {
  const remainingMs = useCooldownRemaining(nextRefreshAt);
  const cooling = remainingMs > 0;
  const disabled = busy || cooling;
  return (
    <span className="flex items-center gap-2">
      <button
        type="button"
        title="Re-run analysis"
        onClick={onRefresh}
        disabled={disabled}
        aria-disabled={disabled}
        className="min-h-11 rounded-xl px-4 text-xs font-semibold text-white disabled:opacity-60"
        style={{ background: "var(--accent)" }}
        data-testid="btn-refresh"
      >
        {busy ? "Starting…" : "Refresh analysis"}
      </button>
      {cooling && (
        <span className="text-xs num" style={{ color: "var(--sub)" }}
          data-testid="refresh-cooldown">
          Updated just now · next refresh in ~{Math.max(1, Math.ceil(remainingMs / 60_000))}m
        </span>
      )}
    </span>
  );
}

/** Remaining cooldown vs the client clock, re-evaluated every ~30s so the
 * button enables itself once next_refresh_at passes. */
function useCooldownRemaining(nextRefreshAt: string | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!nextRefreshAt) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, [nextRefreshAt]);
  if (!nextRefreshAt) return 0;
  const t = Date.parse(nextRefreshAt);
  return Number.isNaN(t) ? 0 : Math.max(0, t - now);
}
