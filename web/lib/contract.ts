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
  disclaimer: string;
  disclaimer_version: string;
}

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

export function fmtSigned(n: number, digits = 2): string {
  return `${n >= 0 ? "+" : ""}${n.toFixed(digits)}`;
}
