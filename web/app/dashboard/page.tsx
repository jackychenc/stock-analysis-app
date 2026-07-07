"use client";

/** Dashboard — three golden states + honest empty-state (task #17).
 * Client reads server booleans/fields only (composite_call / reduced_confidence
 * / conflict_flag); it NEVER computes spread, renormalisation or suppression.
 * Field bindings + rendering rules: A4 web-slice-handoff.md §3. */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  CALL_LABELS,
  Dashboard,
  fmtSigned,
  MODULE_LABELS,
  ModuleName,
  PerModuleBreakdown,
  signalColorVar,
  signalIcon,
} from "@/lib/contract";

const MODULE_ORDER: ModuleName[] = ["technical", "fundamental", "chip", "news"];

export default function DashboardPage() {
  const router = useRouter();
  const [ticker, setTicker] = useState("GF-A");
  const [query, setQuery] = useState("GF-A");
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
          setError(
            body?.detail?.code === "SECTOR_NOT_COVERED"
              ? "This ticker is not covered yet."
              : "Ticker not found.",
          );
          return;
        }
        if (!r.ok) {
          setData(null);
          setError(`Service unavailable (${r.status}).`);
          return;
        }
        setData((await r.json()) as Dashboard);
      } catch {
        setData(null);
        setError("Cannot reach the API — is the local stack running?");
      } finally {
        setBusy(false);
      }
    },
    [router],
  );

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
            setQuery(ticker.trim().toUpperCase());
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
            className="min-h-11 rounded-xl px-4 text-sm font-semibold text-white disabled:opacity-60"
            style={{ background: "var(--accent)" }}
          >
            {busy ? "Loading…" : "Analyze"}
          </button>
        </form>
      </header>

      {error && (
        <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
          {error}
        </div>
      )}

      {data && <DashboardView data={data} />}
    </div>
  );
}

function DashboardView({ data }: { data: Dashboard }) {
  const rec = data.recommendation;

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
          <div className="flex items-center gap-3">
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
                style={{ background: "var(--conf-hi-bg)", color: "var(--conf-hi-ink)" }}
              >
                Confidence: {rec.confidence_level}
                {rec.confidence_pct !== null && ` ${trimPct(rec.confidence_pct)}%`}
              </span>
            )}
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-6">
          <div>
            <div
              className="text-xl font-bold"
              style={{ color: suppressed ? "var(--sub)" : signalColorVar(rec.composite_signal) }}
              data-testid="call-label"
            >
              {!suppressed && (
                <span aria-hidden>{signalIcon(rec.composite_signal)}&nbsp;</span>
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
          {renormalised && (
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
