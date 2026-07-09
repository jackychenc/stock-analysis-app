"use client";

/** Decisions tab (task #14 block 5, wireframe decision-log-S9.png) — the
 * append-only decision history over GET /recommendations/log.
 * - Each row = an immutable recommendation + the LATEST annotation (FR-50:
 *   annotations link to the rec, never mutate it — no in-place rec editing).
 * - The user's price is visually labeled "user-entered" (provenance, FR-49).
 * - "Log / Update" re-opens the modal: the API supports re-annotating a
 *   ticker/date (append-only insert; the list shows the newest), so notes
 *   are editable only by appending — the rec itself never changes.
 * - logged-at is NOT exposed by the API payload; rendered as "—", never
 *   fabricated. */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import DecisionModal from "@/components/DecisionModal";
import {
  CALL_LABELS,
  CALL_RENDER,
  Decision,
  DECISION_LABELS,
  fmtSigned,
  RecommendationLogEntry,
} from "@/lib/contract";

type Filter = "all" | Decision;
const FILTERS: Filter[] = ["all", "followed", "ignored", "partial"];

export default function DecisionsPage() {
  const router = useRouter();
  const [entries, setEntries] = useState<RecommendationLogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [modal, setModal] = useState<RecommendationLogEntry | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await fetch("/api/v1/recommendations/log?limit=100", {
        credentials: "include",
      });
      if (r.status === 401) {
        router.push("/");
        return;
      }
      if (!r.ok) {
        setError(`Service unavailable (${r.status}).`);
        return;
      }
      setEntries((await r.json()) as RecommendationLogEntry[]);
    } catch {
      setError("Cannot reach the API — is the local stack running?");
    }
  }, [router]);

  useEffect(() => {
    void load();
  }, [load]);

  const visible = (entries ?? []).filter(
    (e) => filter === "all" || e.annotation?.decision === filter,
  );

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-extrabold">Your decisions</h1>
        <div role="group" aria-label="Filter by decision"
          className="flex gap-1 rounded-xl p-1" style={{ background: "var(--line-2)" }}>
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              aria-pressed={filter === f}
              onClick={() => setFilter(f)}
              className="min-h-9 rounded-lg px-3 text-xs font-semibold"
              style={
                filter === f
                  ? { background: "var(--card)", color: "var(--ink)", boxShadow: "var(--shadow)" }
                  : { color: "var(--sub)" }
              }
            >
              {f === "all" ? "All" : DECISION_LABELS[f]}
            </button>
          ))}
        </div>
      </header>

      {error && (
        <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
          {error}
        </div>
      )}

      {!error && entries === null && (
        <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
          Loading…
        </div>
      )}

      {entries !== null && entries.length === 0 && (
        <section className="card flex flex-col items-center gap-2 p-10 text-center"
          data-testid="decisions-empty">
          <p className="text-sm font-semibold">No decisions logged yet</p>
          <p className="text-sm" style={{ color: "var(--sub)" }}>
            Log your first decision from a ticker&apos;s recommendation card.
          </p>
          <Link href="/dashboard"
            className="mt-2 rounded-xl px-5 py-2.5 text-sm font-semibold text-white"
            style={{ background: "var(--accent)" }}>
            Go to Dashboard
          </Link>
        </section>
      )}

      {entries !== null && entries.length > 0 && (
        <section className="card overflow-x-auto p-2 sm:p-4">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="text-[10.5px] uppercase tracking-wide" style={{ color: "var(--sub)" }}>
                <th className="px-2 py-2 font-semibold">Ticker</th>
                <th className="px-2 py-2 font-semibold">Rec (as-of)</th>
                <th className="px-2 py-2 font-semibold">Call</th>
                <th className="px-2 py-2 font-semibold">You</th>
                <th className="px-2 py-2 font-semibold">Your price</th>
                <th className="px-2 py-2 font-semibold">Notes</th>
                <th className="px-2 py-2 font-semibold">Logged</th>
                <th className="px-2 py-2"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {visible.map((e) => (
                <Row key={`${e.ticker}-${e.rec_date}`} entry={e}
                  onAnnotate={() => setModal(e)} />
              ))}
              {visible.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-2 py-6 text-center text-sm"
                    style={{ color: "var(--sub)" }}>
                    No rows match this filter.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <p className="mt-2 px-2 text-[10.5px]" style={{ color: "var(--sub)" }}>
            Append-only history: each row shows the immutable recommendation
            with your latest annotation. Updating appends a new annotation —
            the recommendation itself never changes. Prices are user-entered
            (manual, no broker link); logged-at isn&apos;t exposed by the API.
          </p>
        </section>
      )}

      {modal && (
        <DecisionModal
          ticker={modal.ticker}
          recDate={modal.rec_date}
          context={`${CALL_LABELS[modal.composite_call]} call, as of ${modal.rec_date}`}
          onClose={() => setModal(null)}
          onSaved={() => void load()}
        />
      )}
    </div>
  );
}

function Row({
  entry, onAnnotate,
}: {
  entry: RecommendationLogEntry; onAnnotate: () => void;
}) {
  const a = entry.annotation;
  return (
    <tr className="border-t align-top" style={{ borderColor: "var(--line-2)" }}>
      <td className="num px-2 py-2.5 font-semibold">{entry.ticker}</td>
      <td className="num px-2 py-2.5">{entry.rec_date}</td>
      <td className="px-2 py-2.5">
        <span className="font-semibold" style={{ color: CALL_RENDER[entry.composite_call].color }}>
          {CALL_RENDER[entry.composite_call].icon && (
            <span aria-hidden>{CALL_RENDER[entry.composite_call].icon}&nbsp;</span>
          )}
          {CALL_LABELS[entry.composite_call]}
        </span>
        {entry.composite_signal !== null && (
          <span className="num ml-1.5 text-xs" style={{ color: "var(--sub)" }}>
            {fmtSigned(entry.composite_signal)}
          </span>
        )}
      </td>
      <td className="px-2 py-2.5">
        {a ? <DecisionBadge decision={a.decision} /> : <span style={{ color: "var(--sub)" }}>—</span>}
      </td>
      <td className="px-2 py-2.5">
        {a?.transaction_price != null ? (
          <span className="num font-semibold">
            {a.transaction_price.toFixed(2)}
            {/* FR-49 provenance: manual entry, not a market/broker price */}
            <span className="ml-1 rounded px-1 py-0.5 text-[10.5px] font-semibold"
              style={{ background: "var(--line-2)", color: "var(--sub)" }}>
              user-entered
            </span>
          </span>
        ) : (
          <span style={{ color: "var(--sub)" }}>—</span>
        )}
      </td>
      <td className="max-w-56 px-2 py-2.5 text-xs" style={{ color: "var(--sub)" }}>
        {a?.notes ?? "—"}
      </td>
      {/* logged_at is not in the API payload — honest "—", never fabricated */}
      <td className="px-2 py-2.5" style={{ color: "var(--sub)" }}>—</td>
      <td className="px-2 py-2.5">
        <button
          type="button"
          onClick={onAnnotate}
          className="rounded-lg border px-2.5 py-1 text-xs font-semibold"
          style={{ borderColor: "var(--line)", color: "var(--accent)" }}
          data-testid="btn-annotate-row"
        >
          {a ? "Update" : "Log"}
        </button>
      </td>
    </tr>
  );
}

function DecisionBadge({ decision }: { decision: Decision }) {
  const style =
    decision === "followed"
      ? { background: "var(--conf-hi-bg)", color: "var(--conf-hi-ink)" }
      : decision === "partial"
        ? { background: "var(--amber-bg)", color: "var(--amber)" }
        : { background: "var(--line-2)", color: "var(--sub)" };
  return (
    <span className="rounded px-1.5 py-0.5 text-[10.5px] font-semibold" style={style}>
      {DECISION_LABELS[decision].toLowerCase()}
    </span>
  );
}
