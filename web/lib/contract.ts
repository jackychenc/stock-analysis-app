/** Contract v1.2.4 payload types consumed by the web slice (task #17).
 *  Client reads server booleans/fields — it NEVER computes spread,
 *  renormalisation or suppression (A4 handoff §3 state rules). */

export type ModuleName = "technical" | "fundamental" | "chip" | "news";
export type ModuleStatus = "ok" | "unavailable";

export interface PerModuleBreakdown {
  module: ModuleName;
  signal_score: number | null;
  weight_assigned: number;
  weight_effective: number;
  status: ModuleStatus;
}

export interface TargetPrice {
  bear: number | null;
  base: number | null;
  bull: number | null;
}

export interface Recommendation {
  composite_signal: number | null; // null iff SUPPRESSED
  composite_call:
    | "STRONG_SELL" | "SELL" | "HOLD" | "BUY" | "STRONG_BUY" | "SUPPRESSED";
  target_price: TargetPrice | null;
  confidence_level: "HIGH" | "MEDIUM" | "LOW" | null;
  confidence_pct: number | null;
  conflict_flag: boolean;
  reduced_confidence: boolean;
  horizon_months: number;
  data_completeness: number;
  methodology_version: string;
  per_module_breakdown: PerModuleBreakdown[];
  suppressed_reason: string | null;
}

export interface ModuleSummary {
  status: ModuleStatus;
  signal_score: number | null;
  headline_metric: string | null;
}

export interface Dashboard {
  ticker: string;
  rec_date: string | null;
  recommendation: Recommendation | null;
  modules: Record<ModuleName, ModuleSummary>;
  supply_chain_available: boolean;
  /** ADR-009 (task #20): server-authoritative Refresh availability — ISO
   * timestamp while inside the on-demand cooldown, null once available. */
  next_refresh_at: string | null;
  disclaimer: string;
  disclaimer_version: string;
}

/* ---- On-demand analysis (task #20/#22, ADR-009 job polling) ------------- */

export type AnalyzePhase = "fetching" | "scoring";
export type AnalyzeJobStatus = "queued" | "running" | "ready" | "partial" | "failed";
export type AnalyzeFailureReason = "source_unavailable" | "fetch_failed" | "timeout";

/** GET /analyze/{run_id} — phase only while running; reason only when failed
 * (always a sanitized category, never internals). */
export interface AnalyzeJob {
  run_id: string;
  ticker: string;
  status: AnalyzeJobStatus;
  phase?: AnalyzePhase;
  reason?: AnalyzeFailureReason;
}

/** POST /analyze → 202 body. */
export interface AnalyzeAccepted {
  run_id: string;
  status: "queued";
  poll_after_ms: number;
}

/** Mirrors the server SYMBOL_RE ingress guard for instant client feedback —
 * the server 400 remains authoritative (its message is rendered verbatim). */
export const SYMBOL_RE = /^[A-Za-z0-9.\-]{1,12}$/;

/** FR-62: one user-facing line per sanitized failure category (A4 spec state 5)
 * — never raw internals, PII or stacktraces. */
export const FAILURE_COPY: Record<AnalyzeFailureReason, string> = {
  source_unavailable: "A data source is temporarily unavailable.",
  fetch_failed: "We couldn't retrieve data for this ticker.",
  timeout: "Analysis took too long — try again.",
};

export const MODULE_LABELS: Record<ModuleName, string> = {
  technical: "Technical",
  fundamental: "Fundamental",
  chip: "Chip / Institutional",
  news: "News",
};

/** A4 §2: |signal| ≥ 1.5 → double arrow; 0 < |signal| < 1.5 → single; 0 → hold. */
export function signalIcon(score: number | null): string {
  if (score === null || score === 0) return "▬";
  const strong = Math.abs(score) >= 1.5;
  if (score > 0) return strong ? "▲▲" : "▲";
  return strong ? "▼▼" : "▼";
}

export function signalColorVar(score: number | null): string {
  if (score === null || score === 0) return "var(--sig-hold)";
  if (score >= 1.5) return "var(--sig-sb)";
  if (score > 0) return "var(--sig-b)";
  if (score <= -1.5) return "var(--sig-ss)";
  return "var(--sig-s)";
}

export const CALL_LABELS: Record<Recommendation["composite_call"], string> = {
  STRONG_BUY: "Strong Buy",
  BUY: "Buy",
  HOLD: "Hold",
  SELL: "Sell",
  STRONG_SELL: "Strong Sell",
  SUPPRESSED: "No recommendation",
};

/** A4 redline F1: the CALL glyph/color derive from composite_call, never from
 * the sign of composite_signal — a Hold must read neutral, not bullish. */
export const CALL_RENDER: Record<
  Recommendation["composite_call"],
  { icon: string; color: string }
> = {
  STRONG_BUY: { icon: "▲▲", color: "var(--sig-sb)" },
  BUY: { icon: "▲", color: "var(--sig-b)" },
  HOLD: { icon: "▬", color: "var(--sig-hold)" },
  SELL: { icon: "▼", color: "var(--sig-s)" },
  STRONG_SELL: { icon: "▼▼", color: "var(--sig-ss)" },
  SUPPRESSED: { icon: "", color: "var(--sub)" },
};

export function fmtSigned(n: number, digits = 2): string {
  return `${n >= 0 ? "+" : ""}${n.toFixed(digits)}`;
}
