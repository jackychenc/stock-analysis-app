"use client";

/** Backtest page (task #14 block 2, S8) — renders GET /stocks/{t}/backtest
 * with the contract-§10 honesty rules:
 * - full-segment accuracy is the headline, "full-data days only";
 * - the partial segment is a separate, clearly-labeled block — NEVER blended
 *   visually with the headline;
 * - estimated_return carries the exact dual-basis label;
 * - insufficient_history=true renders an honest state card, never a blank or
 *   a fake number;
 * - the A8/A6 compliance block sits adjacent to the figures, not buried.
 * Route: /backtest?symbol=X (linked from the ticker's Dashboard). */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  BacktestResult,
  fmtSigned,
  SYMBOL_RE,
  WINDOW_OPTIONS,
  WindowMonths,
} from "@/lib/contract";

export default function BacktestPage() {
  const router = useRouter();
  const [symbol, setSymbol] = useState<string | null>(null);
  const [windowMonths, setWindowMonths] = useState<WindowMonths>(6);
  const [data, setData] = useState<BacktestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Same deep-link convention as the dashboard: read the query in an effect
  // (no useSearchParams — avoids a Suspense boundary at prerender time).
  useEffect(() => {
    const s = new URLSearchParams(window.location.search).get("symbol");
    setSymbol(s ? s.trim().toUpperCase() : "");
  }, []);

  const load = useCallback(
    async (sym: string, w: WindowMonths) => {
      setBusy(true);
      setError(null);
      try {
        const r = await fetch(
          `/api/v1/stocks/${encodeURIComponent(sym)}/backtest?window_months=${w}`,
          { credentials: "include" },
        );
        if (r.status === 401) {
          router.push("/");
          return;
        }
        if (r.status === 404) {
          setData(null);
          setError(`${sym} isn't covered — run it from the Dashboard first.`);
          return;
        }
        if (!r.ok) {
          setData(null);
          setError(`Service unavailable (${r.status}).`);
          return;
        }
        setData((await r.json()) as BacktestResult);
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
    if (symbol && SYMBOL_RE.test(symbol)) void load(symbol, windowMonths);
  }, [symbol, windowMonths, load]);

  if (symbol === null) return null; // first client paint — query not read yet

  if (!symbol || !SYMBOL_RE.test(symbol)) {
    return (
      <section className="card flex flex-col items-center gap-2 p-10 text-center">
        <p className="text-sm font-semibold">No ticker selected</p>
        <p className="text-sm" style={{ color: "var(--sub)" }}>
          Open Backtest from a ticker&apos;s dashboard.
        </p>
        <Link
          href="/dashboard"
          className="mt-2 rounded-xl px-5 py-2.5 text-sm font-semibold text-white"
          style={{ background: "var(--accent)" }}
        >
          Go to Dashboard
        </Link>
      </section>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link href={`/dashboard?ticker=${encodeURIComponent(symbol)}`}
            className="text-xs font-semibold" style={{ color: "var(--accent)" }}>
            ‹ Dashboard
          </Link>
          <h1 className="text-lg font-extrabold">
            Backtest — <span className="num">{symbol}</span>
          </h1>
        </div>
        <WindowSelector value={windowMonths} onChange={setWindowMonths} busy={busy} />
      </header>

      {error && (
        <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
          {error}
        </div>
      )}

      {!error && data && (
        <>
          {data.insufficient_history ? (
            <InsufficientHistoryCard />
          ) : (
            <FiguresCard data={data} />
          )}
          {/* A8/A6 hard gate: compliance + methodology disclosure ADJACENT to
              the figures (rendered immediately below, same viewport). */}
          <ComplianceBlock data={data} />
        </>
      )}

      {!error && !data && busy && (
        <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
          Loading…
        </div>
      )}
    </div>
  );
}

function WindowSelector({
  value, onChange, busy,
}: {
  value: WindowMonths; onChange: (w: WindowMonths) => void; busy: boolean;
}) {
  return (
    <div role="group" aria-label="Backtest window" className="flex gap-1 rounded-xl p-1"
      style={{ background: "var(--line-2)" }}>
      {WINDOW_OPTIONS.map((w) => (
        <button
          key={w}
          type="button"
          disabled={busy}
          aria-pressed={value === w}
          onClick={() => onChange(w)}
          className="min-h-9 rounded-lg px-3 text-sm font-semibold"
          style={
            value === w
              ? { background: "var(--card)", color: "var(--ink)", boxShadow: "var(--shadow)" }
              : { color: "var(--sub)" }
          }
        >
          {w}M
        </button>
      ))}
    </div>
  );
}

/** §10 honest state — <12 months of recommendation history (global clock):
 * no accuracy number is shown at all, never a blank or a fabricated figure. */
function InsufficientHistoryCard() {
  return (
    <section className="card flex flex-col items-center gap-2 p-10 text-center"
      data-testid="backtest-insufficient">
      <p className="text-base font-semibold">
        Insufficient history — accuracy not yet measurable
      </p>
      <p className="text-sm" style={{ color: "var(--sub)" }}>
        (needs ≥12 months of recommendations)
      </p>
    </section>
  );
}

function FiguresCard({ data }: { data: BacktestResult }) {
  return (
    <section className="card p-6" style={{ borderRadius: "var(--r-hero)" }}
      data-testid="backtest-figures">
      {/* Headline — FULL segment ONLY */}
      <div>
        <div className="text-[10.5px] uppercase tracking-wide" style={{ color: "var(--sub)" }}>
          Rolling accuracy — full-data days only
        </div>
        <div className="num text-3xl font-extrabold" data-testid="accuracy-full">
          {fmtPct(data.rolling_accuracy_full)}
        </div>
      </div>

      {/* Partial segment — separate, clearly segmented, never blended with
          the headline (contract §10 never-blend rule) */}
      <div className="mt-4 rounded-xl border p-3"
        style={{ borderColor: "var(--line)", background: "var(--line-2)" }}
        data-testid="accuracy-partial">
        <div className="text-[10.5px] uppercase tracking-wide" style={{ color: "var(--sub)" }}>
          Partial-data days (separate segment — never blended into the headline)
        </div>
        <div className="num text-xl font-bold">{fmtPct(data.rolling_accuracy_partial)}</div>
      </div>

      <div className="mt-5 grid gap-4 border-t pt-4 sm:grid-cols-2"
        style={{ borderColor: "var(--line-2)" }}>
        <div>
          <div className="text-[10.5px] uppercase tracking-wide" style={{ color: "var(--sub)" }}>
            Estimated return (vs {data.benchmark})
          </div>
          <div className="num text-xl font-bold" data-testid="estimated-return">
            {data.estimated_return == null ? "—" : `${fmtSigned(data.estimated_return)} pp`}
          </div>
          {/* EXACT dual-basis label (A8 hard gate) */}
          <p className="mt-1 text-[10.5px]" style={{ color: "var(--amber)" }}>
            Estimated return spans full+partial days; accuracy is segment-split
          </p>
        </div>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <Meta label="Sample size" value="—" note="not exposed by this endpoint" />
          <Meta label="Benchmark" value={data.benchmark} />
          <Meta label="Window" value={`${data.window_months} months`} />
          <Meta label="Methodology" value={data.methodology_version} />
        </dl>
      </div>
    </section>
  );
}

function Meta({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <>
      <dt className="text-[10.5px] uppercase tracking-wide self-center"
        style={{ color: "var(--sub)" }}>
        {label}
      </dt>
      <dd className="num font-semibold">
        {value}
        {note && (
          <span className="ml-1 text-[10.5px] font-normal" style={{ color: "var(--sub)" }}>
            ({note})
          </span>
        )}
      </dd>
    </>
  );
}

/** A8/A6 compliance block — rendered directly under the figures on every
 * response (including the insufficient-history state). */
function ComplianceBlock({ data }: { data: BacktestResult }) {
  return (
    <section className="card p-5" data-testid="backtest-compliance">
      <p className="text-sm font-semibold">
        Past performance does not guarantee future results. Backtested figures
        are hypothetical model outputs.
      </p>
      <ul className="mt-3 list-disc pl-5 text-xs leading-relaxed"
        style={{ color: "var(--sub)" }}>
        <li>
          Lookback window: <span className="num">{data.window_months}</span> months (rolling).
        </li>
        <li>
          Benchmark: <span className="num">{data.benchmark}</span> (market-matched;
          ^TWII for TW listings, ^GSPC otherwise).
        </li>
        <li>
          Accuracy basis: full-data days and partial-data days are measured as
          separate segments and never blended; the estimated return spans both.
        </li>
        <li>
          Methodology version: <span className="num">{data.methodology_version}</span>.
        </li>
        <li>
          Excludes trading costs, slippage and taxes; no survivorship adjustment.
        </li>
      </ul>
    </section>
  );
}

function fmtPct(v: number | null): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}
