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
  /** v1.2.6 §9 intra-module completeness — distinct from availability. */
  subfields_complete?: boolean;
  /** The honest WHY (e.g. chip "13F baseline captured — direction available
   * next quarter"). Rendered verbatim; never synthesized client-side. */
  subfields_note?: string | null;
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

/* ---- Task #14 blocks 2–5: backtest, config, lens detail, decision log ---- */

export type WindowMonths = 3 | 6 | 12;
export const WINDOW_OPTIONS: WindowMonths[] = [3, 6, 12];

/** GET /stocks/{t}/backtest — contract §10. Accuracies are 0..1 fractions,
 * split by completeness segment and NEVER blended; estimated_return is mean
 * benchmark-relative excess in percentage points over full+partial days.
 * NOTE: the payload does NOT carry sample_size (BacktestResult contract). */
export interface BacktestResult {
  window_months: WindowMonths;
  rolling_accuracy_full: number | null;
  rolling_accuracy_partial: number | null;
  estimated_return: number | null;
  benchmark: string;
  insufficient_history: boolean;
  methodology_version: string;
  disclaimer: string;
  disclaimer_version: string;
}

/** GET/PUT /config/weights. */
export interface ModuleWeights {
  technical: number;
  fundamental: number;
  chip: number;
  news: number;
}

export interface WeightConfig {
  module_weights: ModuleWeights;
  horizon_months: WindowMonths;
}

/** GET /stocks/{t}/technical|fundamentals|chip|news — base lens envelope;
 * series rows are per-lens shapes (below). */
export interface ModuleDetail {
  module: string;
  status: ModuleStatus;
  signal_score: number | null;
  as_of: string | null;
  series: Record<string, unknown>[];
}

export interface TechnicalBar {
  date: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
  volume?: number | null;
  ma20?: number | null;
  ma60?: number | null;
  rsi14?: number | null;
  macd?: number | null;
  macd_signal?: number | null;
  macd_hist?: number | null;
}

export interface FundamentalRow {
  as_of: string;
  pe: number | null;
  pb: number | null;
  ev_ebitda: number | null;
  revenue: number | null;
  eps: number | null;
  gross_margin: number | null;
  op_margin: number | null;
  net_margin: number | null;
}

export interface ChipTwRow {
  trade_date: string;
  foreign_net: number | null;
  investment_trust_net: number | null;
  dealer_net: number | null;
  margin_balance: number | null;
  block_trade_volume: number | null;
  score: number | null;
}

export interface ChipUsRow {
  quarter: string;
  total_shares: number;
  filer_count: number;
}

export interface NewsItem {
  published_at: string;
  headline: string;
  url: string;
  source_name: string;
  sentiment: number | null;
}

/* ---- Decision log (FR-49/FR-50) ---- */

export type Decision = "followed" | "ignored" | "partial";

export const DECISION_LABELS: Record<Decision, string> = {
  followed: "Followed",
  ignored: "Ignored",
  partial: "Partial",
};

/** POST /recommendations/log/{rec_date}/annotate?ticker=… body. */
export interface DecisionAnnotation {
  decision: Decision;
  transaction_price?: number | null;
  notes?: string | null;
}

/** GET /recommendations/log entry — the immutable rec plus the latest
 * append-only annotation (FR-50: annotations never mutate the rec). */
export interface RecommendationLogEntry extends Recommendation {
  ticker: string;
  rec_date: string;
  annotation: DecisionAnnotation | null;
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

/** Nullable metric render rule (S3–S6): "—" for absent-but-missing-flagged;
 * never a fabricated value. */
export function fmtNum(v: number | null | undefined, digits = 2): string {
  return v == null ? "—" : v.toFixed(digits);
}

/** Compact large figures (revenue, share counts) with tabular honesty. */
export function fmtCompact(v: number | null | undefined): string {
  if (v == null) return "—";
  return new Intl.NumberFormat("en", {
    notation: "compact", maximumFractionDigits: 2,
  }).format(v);
}
