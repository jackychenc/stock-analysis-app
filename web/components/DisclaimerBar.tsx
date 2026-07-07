"use client";

import { useState } from "react";

/** FR-39 canonical text (fr39-v1) — matches the server DISCLAIMER constant;
 * the dashboard additionally verifies the payload value it receives. */
export const CANONICAL_DISCLAIMER =
  "For personal decision-support and educational use only. Not personalized " +
  "investment advice, and not a solicitation or recommendation to buy or sell " +
  "any security. Not provided by a registered investment adviser (US Investment " +
  "Advisers Act) or a Securities Investment Consulting Enterprise (Taiwan). " +
  "Signals, scores and target prices are model outputs; past performance and " +
  "backtests are hypothetical and do not guarantee future results. You are " +
  "solely responsible for your own investment decisions; consult a licensed " +
  "adviser.";

/** Persistent, non-hideable disclaimer bar (FR-39; A4 handoff §4).
 * Concise line always visible; full canonical text on expand. */
export default function DisclaimerBar({ fullText }: { fullText?: string }) {
  const [expanded, setExpanded] = useState(false);
  const canonical = fullText ?? CANONICAL_DISCLAIMER;
  return (
    <footer className="fixed inset-x-0 bottom-0 z-50 border-t bg-white/95 backdrop-blur"
      style={{ borderColor: "var(--line)" }}>
      {/* A4 redline: #334155 keeps WCAG AA >=4.5:1 on the translucent bar */}
      {expanded && (
        <div className="mx-auto max-w-5xl px-4 pt-3 text-xs leading-relaxed"
          style={{ color: "#334155" }} data-testid="disclaimer-full">
          {canonical}
        </div>
      )}
      <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-center gap-2 px-4 py-2 text-center text-xs"
        style={{ color: "#334155" }}>
        <span>
          For personal decision-support &amp; educational use only — not investment
          advice; model outputs, not from a registered adviser.
        </span>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="min-h-6 shrink-0 font-medium underline-offset-2 hover:underline"
          style={{ color: "var(--accent)" }}
          aria-expanded={expanded}
        >
          {expanded ? "Hide ‹" : "Full disclaimer ›"}
        </button>
      </div>
    </footer>
  );
}
