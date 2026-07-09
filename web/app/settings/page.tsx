"use client";

/** Settings page (task #14 block 3, S10) — module-weights editor with LIVE
 * sum validation (must equal 1.0 ±0.001, matching the server's tolerance;
 * Save disabled until valid), horizon selector 3/6/12, and the coverage-pool
 * section. Server validation errors (422 detail.message) render verbatim —
 * the server stays authoritative.
 *
 * Coverage pool: there is NO ticker-list/coverage API, so the section is
 * read-only informational — it states the cap (25, the server default) and
 * the reject-over-evict policy matching the at-cap 409 message; the covered
 * count is not derivable client-side and is honestly not shown. */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  MODULE_LABELS,
  ModuleName,
  WeightConfig,
  WINDOW_OPTIONS,
  WindowMonths,
} from "@/lib/contract";

const MODULE_ORDER: ModuleName[] = ["technical", "fundamental", "chip", "news"];
const SUM_TOLERANCE = 1e-3; // mirrors the server's math.isclose(…, abs_tol=1e-3)

export default function SettingsPage() {
  const router = useRouter();
  // Weights kept as strings so typing "0.2" doesn't fight the input.
  const [weights, setWeights] = useState<Record<ModuleName, string> | null>(null);
  const [horizon, setHorizon] = useState<WindowMonths>(6);
  const [error, setError] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/api/v1/config/weights", { credentials: "include" });
        if (cancelled) return;
        if (r.status === 401) {
          router.push("/");
          return;
        }
        if (!r.ok) {
          setError(`Service unavailable (${r.status}).`);
          return;
        }
        const cfg = (await r.json()) as WeightConfig;
        if (cancelled) return;
        setWeights({
          technical: String(cfg.module_weights.technical),
          fundamental: String(cfg.module_weights.fundamental),
          chip: String(cfg.module_weights.chip),
          news: String(cfg.module_weights.news),
        });
        setHorizon(cfg.horizon_months);
      } catch {
        if (!cancelled) setError("Cannot reach the API — is the local stack running?");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  const parsed = weights
    ? MODULE_ORDER.map((m) => Number(weights[m]))
    : [];
  const allNumeric =
    parsed.length === 4 && parsed.every((v) => Number.isFinite(v) && v >= 0 && v <= 1);
  const sum = allNumeric ? parsed.reduce((a, b) => a + b, 0) : NaN;
  const sumValid = allNumeric && Math.abs(sum - 1.0) <= SUM_TOLERANCE;

  const save = useCallback(async () => {
    if (!weights || !sumValid) return;
    setSaving(true);
    setServerError(null);
    setSaved(false);
    try {
      const body: WeightConfig = {
        module_weights: {
          technical: Number(weights.technical),
          fundamental: Number(weights.fundamental),
          chip: Number(weights.chip),
          news: Number(weights.news),
        },
        horizon_months: horizon,
      };
      const r = await fetch("/api/v1/config/weights", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        credentials: "include",
      });
      if (r.status === 401) {
        router.push("/");
        return;
      }
      if (!r.ok) {
        // Render the server's validation message VERBATIM (it's authoritative).
        const payload = await r.json().catch(() => null);
        const message =
          payload?.detail?.message ??
          (typeof payload?.detail === "string" ? payload.detail : null) ??
          `Save failed (${r.status}).`;
        setServerError(message);
        return;
      }
      const cfg = (await r.json()) as WeightConfig;
      setWeights({
        technical: String(cfg.module_weights.technical),
        fundamental: String(cfg.module_weights.fundamental),
        chip: String(cfg.module_weights.chip),
        news: String(cfg.module_weights.news),
      });
      setHorizon(cfg.horizon_months);
      setSaved(true);
    } catch {
      setServerError("Cannot reach the API — is the local stack running?");
    } finally {
      setSaving(false);
    }
  }, [weights, sumValid, horizon, router]);

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-extrabold">Settings</h1>

      {error && (
        <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
          {error}
        </div>
      )}

      {!error && !weights && (
        <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
          Loading…
        </div>
      )}

      {weights && (
        <section className="card p-6" style={{ borderRadius: "var(--r-hero)" }}>
          <h2 className="text-sm font-bold">Module weights</h2>
          <p className="mt-1 text-xs" style={{ color: "var(--sub)" }}>
            The four lens weights must sum to exactly 1.0 (±0.001). The next
            daily run uses the saved weights.
          </p>

          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {MODULE_ORDER.map((m) => (
              <label key={m} className="text-sm font-medium">
                {MODULE_LABELS[m]}
                <input
                  type="number"
                  inputMode="decimal"
                  min={0}
                  max={1}
                  step={0.05}
                  className="num mt-1 w-full rounded-lg border px-3 py-2.5 text-sm outline-none focus:ring-2"
                  style={{ borderColor: "var(--line)" }}
                  value={weights[m]}
                  onChange={(e) => {
                    setSaved(false);
                    setWeights({ ...weights, [m]: e.target.value });
                  }}
                  aria-label={`${MODULE_LABELS[m]} weight`}
                />
              </label>
            ))}
          </div>

          {/* LIVE running sum — the validity signal Save is gated on */}
          <div
            role="status"
            data-testid="weights-sum"
            className="mt-4 rounded-lg px-3 py-2 text-sm font-semibold num"
            style={
              sumValid
                ? { background: "var(--conf-hi-bg)", color: "var(--conf-hi-ink)" }
                : { background: "var(--red-bg)", color: "var(--red)" }
            }
          >
            Sum: {Number.isFinite(sum) ? sum.toFixed(3) : "—"}{" "}
            {sumValid ? "✓" : "— must equal 1.000 (±0.001)"}
          </div>

          <div className="mt-6 border-t pt-4" style={{ borderColor: "var(--line-2)" }}>
            <h2 className="text-sm font-bold">Horizon</h2>
            <div role="group" aria-label="Recommendation horizon"
              className="mt-2 inline-flex gap-1 rounded-xl p-1"
              style={{ background: "var(--line-2)" }}>
              {WINDOW_OPTIONS.map((h) => (
                <button
                  key={h}
                  type="button"
                  aria-pressed={horizon === h}
                  onClick={() => {
                    setSaved(false);
                    setHorizon(h);
                  }}
                  className="min-h-9 rounded-lg px-4 text-sm font-semibold"
                  style={
                    horizon === h
                      ? { background: "var(--card)", color: "var(--ink)", boxShadow: "var(--shadow)" }
                      : { color: "var(--sub)" }
                  }
                >
                  {h} months
                </button>
              ))}
            </div>
          </div>

          {serverError && (
            <p role="alert" className="mt-4 rounded-lg px-3 py-2 text-sm"
              style={{ background: "var(--red-bg)", color: "var(--red)" }}>
              {serverError}
            </p>
          )}

          <div className="mt-5 flex items-center gap-3">
            <button
              type="button"
              onClick={() => void save()}
              disabled={!sumValid || saving}
              aria-disabled={!sumValid || saving}
              className="min-h-11 rounded-xl px-5 text-sm font-semibold text-white disabled:opacity-60"
              style={{ background: "var(--accent)" }}
              data-testid="btn-save-weights"
            >
              {saving ? "Saving…" : "Save"}
            </button>
            {saved && (
              <span className="text-sm font-semibold" style={{ color: "var(--conf-hi-ink)" }}
                role="status">
                ✓ Saved
              </span>
            )}
          </div>
        </section>
      )}

      <PoolSection />
    </div>
  );
}

/** Coverage-pool section — read-only informational (no coverage-list API
 * exists; nothing is invented client-side). Policy copy matches the at-cap
 * 409 COVERAGE_POOL_FULL message users may see on Analyze. */
function PoolSection() {
  return (
    <section className="card p-6" data-testid="pool-section">
      <h2 className="text-sm font-bold">Coverage pool</h2>
      <p className="mt-2 text-sm" style={{ color: "var(--sub)" }}>
        Analyzed tickers are kept up-to-date daily; pool capacity 25.
      </p>
      <p className="mt-2 text-sm" style={{ color: "var(--sub)" }}>
        When the pool is full, a new ticker is <strong>rejected</strong> —
        nothing already tracked is ever evicted. Analyzing a new ticker at
        capacity returns “Coverage pool full”; to make room, remove a tracked
        ticker or raise <span className="num">MAX_COVERAGE_POOL_SIZE</span> in
        the server configuration.
      </p>
      <p className="mt-2 text-xs" style={{ color: "var(--sub)" }}>
        The covered-ticker list isn&apos;t exposed by the API yet, so the
        current count can&apos;t be shown here.
      </p>
    </section>
  );
}
