"use client";

/** Decision-log modal (task #14 block 5, wireframe decision-log-S9.png).
 * POST /recommendations/log/{rec_date}/annotate?ticker=… with
 * DecisionAnnotation{decision, transaction_price?, notes?}.
 * FR-49: the price is manual entry (no broker link) and is labeled
 * user-entered everywhere it later renders. FR-50: the annotation LINKS to
 * the recommendation and never mutates it — copy here and in the saved
 * state says so explicitly; the rec itself is immutable history.
 * Re-invoking for the same ticker/date appends a new annotation (the log
 * shows the latest) — still append-only, the rec never changes. */

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Decision, DECISION_LABELS } from "@/lib/contract";

const DECISIONS: Decision[] = ["followed", "ignored", "partial"];
// Client-side decimal pre-check only — the server stays authoritative.
const PRICE_RE = /^\d+(\.\d+)?$/;

export default function DecisionModal({
  ticker, recDate, context, onClose, onSaved,
}: {
  ticker: string;
  recDate: string;
  /** e.g. "Buy call, as of 2026-07-08" — context line under the title. */
  context: string;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const router = useRouter();
  const [decision, setDecision] = useState<Decision | null>(null);
  const [price, setPrice] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  async function save() {
    if (!decision) {
      setError("Choose Followed, Ignored or Partial first.");
      return;
    }
    const trimmed = price.trim();
    if (trimmed && !PRICE_RE.test(trimmed)) {
      setError("Transaction price must be a plain decimal number (e.g. 968.00).");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(
        `/api/v1/recommendations/log/${encodeURIComponent(recDate)}/annotate?ticker=${encodeURIComponent(ticker)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            decision,
            transaction_price: trimmed ? Number(trimmed) : null,
            notes: notes.trim() ? notes.trim() : null,
          }),
          credentials: "include",
        },
      );
      if (r.status === 401) {
        router.push("/");
        return;
      }
      if (!r.ok) {
        const body = await r.json().catch(() => null);
        setError(body?.detail?.message ?? `Save failed (${r.status}).`);
        return;
      }
      setSaved(true);
      onSaved?.();
    } catch {
      setError("Cannot reach the API — is the local stack running?");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Log your decision for ${ticker}`}
        className="card w-full max-w-md p-6"
        style={{ borderRadius: "var(--r-hero)" }}
        data-testid="decision-modal"
      >
        {saved ? (
          <div className="flex flex-col items-center gap-2 py-4 text-center">
            <p className="text-base font-semibold" style={{ color: "var(--conf-hi-ink)" }}>
              ✓ Decision saved
            </p>
            <p className="text-sm" style={{ color: "var(--sub)" }} data-testid="decision-saved">
              Recorded as an annotation linked to this recommendation — the
              recommendation itself is unchanged (immutable history).
            </p>
            <button
              type="button"
              onClick={onClose}
              className="mt-3 min-h-11 rounded-xl px-6 text-sm font-semibold text-white"
              style={{ background: "var(--accent)" }}
            >
              Done
            </button>
          </div>
        ) : (
          <>
            <h2 className="text-base font-extrabold">
              Log your decision · <span className="num">{ticker}</span>
            </h2>
            <p className="mt-1 text-xs" style={{ color: "var(--sub)" }}>
              against the {context}
            </p>

            <div role="radiogroup" aria-label="Your decision"
              className="mt-4 grid grid-cols-3 gap-2">
              {DECISIONS.map((d) => (
                <button
                  key={d}
                  type="button"
                  role="radio"
                  aria-checked={decision === d}
                  onClick={() => setDecision(d)}
                  className="min-h-11 rounded-xl border text-sm font-semibold"
                  style={
                    decision === d
                      ? { borderColor: "var(--accent)", background: "var(--accent)", color: "#fff" }
                      : { borderColor: "var(--line)", color: "var(--sub)" }
                  }
                >
                  {DECISION_LABELS[d]}
                </button>
              ))}
            </div>

            <label className="mt-4 block text-sm font-medium">
              Transaction price (optional) — manual entry, no broker link
              <input
                inputMode="decimal"
                placeholder="e.g. 968.00"
                className="num mt-1 w-full rounded-lg border px-3 py-2.5 text-sm outline-none focus:ring-2"
                style={{ borderColor: "var(--line)" }}
                value={price}
                onChange={(e) => setPrice(e.target.value)}
                data-testid="decision-price"
              />
            </label>

            <label className="mt-3 block text-sm font-medium">
              Notes (optional)
              <textarea
                rows={3}
                className="mt-1 w-full rounded-lg border px-3 py-2.5 text-sm outline-none focus:ring-2"
                style={{ borderColor: "var(--line)" }}
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                data-testid="decision-notes"
              />
            </label>

            <p className="mt-3 text-[10.5px]" style={{ color: "var(--sub)" }}>
              🔒 Price + notes encrypted at rest. This annotation links to the
              recommendation and <strong>never mutates it</strong> — the rec
              stays immutable history.
            </p>

            {error && (
              <p role="alert" className="mt-3 rounded-lg px-3 py-2 text-sm"
                style={{ background: "var(--red-bg)", color: "var(--red)" }}>
                {error}
              </p>
            )}

            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={() => void save()}
                disabled={busy}
                aria-disabled={busy}
                className="min-h-11 flex-1 rounded-xl text-sm font-semibold text-white disabled:opacity-60"
                style={{ background: "var(--sig-sb)" }}
                data-testid="btn-save-decision"
              >
                {busy ? "Saving…" : "Save decision"}
              </button>
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="min-h-11 rounded-xl border px-4 text-sm font-semibold"
                style={{ borderColor: "var(--line)", color: "var(--sub)" }}
              >
                Cancel
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
