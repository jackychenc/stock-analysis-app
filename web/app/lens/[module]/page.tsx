"use client";

/** Lens-detail pages S3–S6 (task #14 block 4, wireframe lens-detail-S3-S6.png)
 * — one dynamic route, /lens/{module}?symbol=X, linked from the dashboard
 * lens cards. Binds GET /stocks/{t}/technical|fundamentals|chip|news
 * (ModuleDetail envelope). Rules:
 * - status unavailable → sanitized reason card, NEVER raw errors (no-leak);
 * - nullable metrics render "—", never a fabricated value;
 * - S5 chip additionally reads the dashboard breakdown's subfields_note so
 *   the "why" ("13F baseline captured — direction available next quarter")
 *   is surfaced, not just a bare unavailable pill;
 * - S3 chart is dependency-free inline SVG with a data-table fallback (a11y).
 */

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  ChipTwRow,
  ChipUsRow,
  Dashboard,
  fmtCompact,
  fmtNum,
  fmtSigned,
  FundamentalRow,
  MODULE_LABELS,
  ModuleDetail,
  ModuleName,
  NewsItem,
  signalColorVar,
  signalIcon,
  SYMBOL_RE,
  TechnicalBar,
} from "@/lib/contract";

/** API path per module (note: module "fundamental" → path "fundamentals"). */
const ENDPOINT: Record<ModuleName, string> = {
  technical: "technical",
  fundamental: "fundamentals",
  chip: "chip",
  news: "news",
};

const MODULES = Object.keys(ENDPOINT) as ModuleName[];

export default function LensDetailPage() {
  const router = useRouter();
  const params = useParams<{ module: string }>();
  const module = MODULES.find((m) => m === params.module) ?? null;

  const [symbol, setSymbol] = useState<string | null>(null);
  const [data, setData] = useState<ModuleDetail | null>(null);
  // S5: the chip card's honest WHY comes from the dashboard breakdown.
  const [chipNote, setChipNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const s = new URLSearchParams(window.location.search).get("symbol");
    setSymbol(s ? s.trim().toUpperCase() : "");
  }, []);

  useEffect(() => {
    if (!module || !symbol || !SYMBOL_RE.test(symbol)) return;
    let cancelled = false;
    setBusy(true);
    setError(null);
    (async () => {
      try {
        const r = await fetch(
          `/api/v1/stocks/${encodeURIComponent(symbol)}/${ENDPOINT[module]}`,
          { credentials: "include" },
        );
        if (cancelled) return;
        if (r.status === 401) {
          router.push("/");
          return;
        }
        if (r.status === 404) {
          setError(`${symbol} isn't covered — analyze it from the Dashboard first.`);
          return;
        }
        if (!r.ok) {
          setError(`Service unavailable (${r.status}).`);
          return;
        }
        const detail = (await r.json()) as ModuleDetail;
        if (cancelled) return;
        setData(detail);
        if (module === "chip") {
          // Fetch the dashboard alongside the detail for the breakdown's
          // subfields_note — the WHY behind an unavailable chip lens.
          try {
            const dr = await fetch(
              `/api/v1/stocks/${encodeURIComponent(symbol)}/dashboard`,
              { credentials: "include" },
            );
            if (!cancelled && dr.ok) {
              const dash = (await dr.json()) as Dashboard;
              const entry = dash.recommendation?.per_module_breakdown.find(
                (b) => b.module === "chip",
              );
              setChipNote(entry?.subfields_note ?? null);
            }
          } catch {
            /* note is best-effort context; the detail still renders */
          }
        }
      } catch {
        if (!cancelled) setError("Cannot reach the API — is the local stack running?");
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [module, symbol, router]);

  if (!module) {
    return (
      <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
        Unknown lens.{" "}
        <Link href="/dashboard" style={{ color: "var(--accent)" }}>
          Back to Dashboard
        </Link>
      </div>
    );
  }
  if (symbol === null) return null; // query not read yet (first client paint)
  if (!symbol || !SYMBOL_RE.test(symbol)) {
    return (
      <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
        No ticker selected — open a lens from a ticker&apos;s{" "}
        <Link href="/dashboard" style={{ color: "var(--accent)" }}>
          Dashboard
        </Link>
        .
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link href={`/dashboard?ticker=${encodeURIComponent(symbol)}`}
            className="text-xs font-semibold" style={{ color: "var(--accent)" }}>
            ‹ Dashboard / {MODULE_LABELS[module]}
          </Link>
          <h1 className="text-lg font-extrabold">
            {MODULE_LABELS[module]} — <span className="num">{symbol}</span>
          </h1>
        </div>
        {data && (
          <div className="flex items-center gap-3">
            {data.as_of && (
              <span className="rounded-lg px-2.5 py-1 text-xs font-semibold num"
                style={{ background: "var(--line-2)", color: "var(--sub)" }}>
                as of {data.as_of}
              </span>
            )}
            {data.signal_score !== null && (
              <span className="num text-lg font-extrabold"
                style={{ color: signalColorVar(data.signal_score) }}>
                <span aria-hidden>{signalIcon(data.signal_score)}&nbsp;</span>
                {fmtSigned(data.signal_score, 1)}
              </span>
            )}
          </div>
        )}
      </header>

      {error && (
        <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
          {error}
        </div>
      )}

      {!error && busy && !data && (
        <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
          Loading…
        </div>
      )}

      {!error && data && data.status === "unavailable" && (
        <UnavailableCard module={module} whyNote={module === "chip" ? chipNote : null} />
      )}

      {!error && data && data.status === "ok" && (
        <>
          {module === "technical" && <TechnicalPanel data={data} />}
          {module === "fundamental" && <FundamentalPanel data={data} />}
          {module === "chip" && <ChipPanel data={data} whyNote={chipNote} />}
          {module === "news" && <NewsPanel data={data} />}
        </>
      )}
    </div>
  );
}

/** Sanitized unavailable card — never raw errors/internals (no-leak rule).
 * For chip, the dashboard breakdown's subfields_note is the honest WHY. */
function UnavailableCard({
  module, whyNote,
}: {
  module: ModuleName; whyNote: string | null;
}) {
  return (
    <section className="card flex flex-col items-center gap-2 p-10 text-center"
      data-testid="lens-unavailable">
      <span className="rounded px-2 py-0.5 text-xs font-semibold"
        style={{ background: "var(--red-bg)", color: "var(--red)" }}>
        data unavailable
      </span>
      <p className="text-sm" style={{ color: "var(--sub)" }}>
        {whyNote ??
          `${MODULE_LABELS[module]} data is currently unavailable for this ticker;
           the composite treats this lens as weight 0.`}
      </p>
    </section>
  );
}

/* ---- S3 Technical --------------------------------------------------------- */

function TechnicalPanel({ data }: { data: ModuleDetail }) {
  const series = data.series as unknown as TechnicalBar[];
  // Indicators are folded onto their calc-date row — read the latest row
  // that carries any of them; missing values render "—", never fabricated.
  const latest =
    [...series].reverse().find(
      (b) => b.ma20 != null || b.ma60 != null || b.rsi14 != null || b.macd != null,
    ) ?? null;
  return (
    <>
      <section className="card p-5">
        <h2 className="text-sm font-bold">
          Close + MA20 / MA60
          <span className="ml-2 text-[10.5px] font-normal" style={{ color: "var(--sub)" }}>
            last {series.length} bars
          </span>
        </h2>
        <PriceChart series={series} />
        <SeriesTable series={series} />
      </section>

      <section className="grid gap-3 sm:grid-cols-3">
        <ReadoutCard label="MA20 / MA60"
          value={`${fmtNum(latest?.ma20)} / ${fmtNum(latest?.ma60)}`} />
        <ReadoutCard label="RSI14" value={fmtNum(latest?.rsi14, 0)} />
        <ReadoutCard
          label="MACD / signal / hist"
          value={`${fmtNum(latest?.macd)} / ${fmtNum(latest?.macd_signal)} / ${fmtNum(latest?.macd_hist)}`}
        />
      </section>
    </>
  );
}

function ReadoutCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="card p-4">
      <div className="text-[10.5px] uppercase tracking-wide" style={{ color: "var(--sub)" }}>
        {label}
      </div>
      <div className="num mt-1 text-lg font-extrabold">{value}</div>
    </div>
  );
}

const CHART_LINES: { key: "close" | "ma20" | "ma60"; label: string; color: string }[] = [
  { key: "close", label: "Close", color: "var(--ink)" },
  { key: "ma20", label: "MA20", color: "var(--accent)" },
  { key: "ma60", label: "MA60", color: "var(--amber)" },
];

/** Dependency-free inline SVG line chart (no chart library). Gaps in a
 * series (nullable values) break the line rather than interpolating. */
function PriceChart({ series }: { series: TechnicalBar[] }) {
  const values: number[] = [];
  for (const b of series) {
    for (const { key } of CHART_LINES) {
      const v = b[key];
      if (v != null) values.push(v);
    }
  }
  if (series.length < 2 || values.length < 2) {
    return (
      <p className="mt-3 text-sm" style={{ color: "var(--sub)" }}>
        Not enough price history to chart.
      </p>
    );
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const pad = (max - min) * 0.06 || 1;
  const lo = min - pad;
  const hi = max + pad;
  const W = 600, H = 220, PX = 6, PY = 6;
  const x = (i: number) => PX + (i / (series.length - 1)) * (W - 2 * PX);
  const y = (v: number) => PY + (1 - (v - lo) / (hi - lo)) * (H - 2 * PY);
  const pathFor = (key: "close" | "ma20" | "ma60") => {
    let d = "";
    let pen = false;
    series.forEach((b, i) => {
      const v = b[key];
      if (v == null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const first = series[0]?.date;
  const last = series[series.length - 1]?.date;
  return (
    <figure className="mt-3">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full"
        role="img"
        aria-label={`Line chart of closing prices with MA20 and MA60 overlays, ${first} to ${last}; full values in the data table below`}
      >
        <rect x="0" y="0" width={W} height={H} fill="var(--line-2)" rx="8" />
        {CHART_LINES.map(({ key, color }) => (
          <path key={key} d={pathFor(key)} fill="none" stroke={color}
            strokeWidth={key === "close" ? 2 : 1.5}
            strokeDasharray={key === "close" ? undefined : "4 3"} />
        ))}
      </svg>
      <figcaption className="mt-2 flex flex-wrap gap-4 text-[10.5px]"
        style={{ color: "var(--sub)" }}>
        {CHART_LINES.map(({ key, label, color }) => (
          <span key={key} className="flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-0.5 w-4"
              style={{ background: color }} />
            {label}
          </span>
        ))}
        <span className="num">
          {first} → {last}
        </span>
      </figcaption>
    </figure>
  );
}

/** A11y fallback: the full series as a real table (collapsed by default). */
function SeriesTable({ series }: { series: TechnicalBar[] }) {
  return (
    <details className="mt-3">
      <summary className="cursor-pointer text-xs font-semibold"
        style={{ color: "var(--accent)" }}>
        View data table
      </summary>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead>
            <tr style={{ color: "var(--sub)" }}>
              {["Date", "Close", "MA20", "MA60", "RSI14", "MACD", "Volume"].map((h) => (
                <th key={h} className="py-1.5 pr-3 font-semibold">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="num">
            {[...series].reverse().map((b) => (
              <tr key={b.date} className="border-t" style={{ borderColor: "var(--line-2)" }}>
                <td className="py-1.5 pr-3">{b.date}</td>
                <td className="py-1.5 pr-3">{fmtNum(b.close)}</td>
                <td className="py-1.5 pr-3">{fmtNum(b.ma20)}</td>
                <td className="py-1.5 pr-3">{fmtNum(b.ma60)}</td>
                <td className="py-1.5 pr-3">{fmtNum(b.rsi14, 0)}</td>
                <td className="py-1.5 pr-3">{fmtNum(b.macd)}</td>
                <td className="py-1.5 pr-3">{b.volume == null ? "—" : fmtCompact(b.volume)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/* ---- S4 Fundamental -------------------------------------------------------- */

function FundamentalPanel({ data }: { data: ModuleDetail }) {
  const row = (data.series[0] ?? null) as unknown as FundamentalRow | null;
  if (!row) {
    return (
      <div className="card p-6 text-sm" style={{ color: "var(--sub)" }}>
        No fundamental snapshot available.
      </div>
    );
  }
  const metrics: { label: string; value: string }[] = [
    { label: "P/E", value: fmtNum(row.pe, 1) },
    { label: "P/B", value: fmtNum(row.pb, 1) },
    { label: "EV/EBITDA", value: fmtNum(row.ev_ebitda, 1) },
    { label: "Revenue", value: fmtCompact(row.revenue) },
    { label: "EPS", value: fmtNum(row.eps) },
    { label: "Gross / Op / Net margin", value: marginTriplet(row) },
  ];
  return (
    <section className="card p-5">
      <h2 className="text-sm font-bold">Metrics</h2>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-[10.5px] uppercase tracking-wide" style={{ color: "var(--sub)" }}>
              <th className="py-2 pr-3 font-semibold">Metric</th>
              <th className="py-2 font-semibold">Value</th>
            </tr>
          </thead>
          <tbody>
            {metrics.map((m) => (
              <tr key={m.label} className="border-t" style={{ borderColor: "var(--line-2)" }}>
                <td className="py-2 pr-3">{m.label}</td>
                <td className="num py-2 font-semibold">{m.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-[10.5px]" style={{ color: "var(--sub)" }}>
        Any nullable metric renders “—” (present-but-missing flagged), never a
        fabricated value.
      </p>
    </section>
  );
}

function marginTriplet(row: FundamentalRow): string {
  const pct = (v: number | null) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);
  return `${pct(row.gross_margin)} / ${pct(row.op_margin)} / ${pct(row.net_margin)}`;
}

/* ---- S5 Chip / Institutional ------------------------------------------------ */

/** Market branch is detected from the series row shape the server persisted:
 * TW rows carry trade_date (3-institution dailies); US rows carry quarter
 * (13F aggregates). Never guessed from the symbol client-side. */
function ChipPanel({ data, whyNote }: { data: ModuleDetail; whyNote: string | null }) {
  const rows = data.series;
  const isTw = rows.length > 0 && "trade_date" in rows[0];
  return (
    <section className="card p-5">
      <h2 className="text-sm font-bold">
        {isTw ? "TW 3-institution dailies" : "US 13F quarter aggregates"}
        <span className="ml-2 rounded px-1.5 py-0.5 text-[10.5px] font-semibold"
          style={{ background: "var(--line-2)", color: "var(--sub)" }}>
          market: {isTw ? "TW" : "US"}
        </span>
      </h2>
      {whyNote && (
        <p className="mt-2 rounded-lg px-3 py-2 text-xs" data-testid="chip-why"
          style={{ background: "var(--amber-bg)", color: "var(--amber)" }}>
          {whyNote}
        </p>
      )}
      {!isTw && !whyNote && (
        <p className="mt-2 text-[10.5px]" style={{ color: "var(--sub)" }}>
          Quarterly positioning (13F, delayed).
        </p>
      )}
      <div className="mt-3 overflow-x-auto">
        {isTw ? <ChipTwTable rows={rows as unknown as ChipTwRow[]} />
              : <ChipUsTable rows={rows as unknown as ChipUsRow[]} />}
      </div>
    </section>
  );
}

function ChipTwTable({ rows }: { rows: ChipTwRow[] }) {
  const net = (v: number | null) =>
    v == null ? "—" : `${v >= 0 ? "+" : ""}${fmtCompact(v)}`;
  return (
    <table className="w-full text-left text-xs">
      <thead>
        <tr className="text-[10.5px] uppercase tracking-wide" style={{ color: "var(--sub)" }}>
          {["Date", "Foreign", "Inv-trust", "Dealer", "Margin bal.", "Block vol."].map((h) => (
            <th key={h} className="py-1.5 pr-3 font-semibold">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody className="num">
        {[...rows].reverse().map((r) => (
          <tr key={r.trade_date} className="border-t" style={{ borderColor: "var(--line-2)" }}>
            <td className="py-1.5 pr-3">{r.trade_date}</td>
            <td className="py-1.5 pr-3">{net(r.foreign_net)}</td>
            <td className="py-1.5 pr-3">{net(r.investment_trust_net)}</td>
            <td className="py-1.5 pr-3">{net(r.dealer_net)}</td>
            <td className="py-1.5 pr-3">{r.margin_balance == null ? "—" : fmtCompact(r.margin_balance)}</td>
            <td className="py-1.5 pr-3">{r.block_trade_volume == null ? "—" : fmtCompact(r.block_trade_volume)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ChipUsTable({ rows }: { rows: ChipUsRow[] }) {
  return (
    <table className="w-full text-left text-xs">
      <thead>
        <tr className="text-[10.5px] uppercase tracking-wide" style={{ color: "var(--sub)" }}>
          {["Quarter", "Total shares (13F)", "Filers"].map((h) => (
            <th key={h} className="py-1.5 pr-3 font-semibold">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody className="num">
        {[...rows].reverse().map((r) => (
          <tr key={r.quarter} className="border-t" style={{ borderColor: "var(--line-2)" }}>
            <td className="py-1.5 pr-3">{r.quarter}</td>
            <td className="py-1.5 pr-3">{fmtCompact(r.total_shares)}</td>
            <td className="py-1.5 pr-3">{r.filer_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/* ---- S6 News ---------------------------------------------------------------- */

function NewsPanel({ data }: { data: ModuleDetail }) {
  const items = data.series as unknown as NewsItem[];
  return (
    <section className="card p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-bold">News · Informational</h2>
        <span className="text-xs num" style={{ color: "var(--sub)" }}>
          {items.length} headline{items.length === 1 ? "" : "s"} in 7-day window
          {data.signal_score !== null && ` · aggregate ${fmtSigned(data.signal_score)}`}
        </span>
      </div>
      {items.length === 0 ? (
        /* fetch-ok with zero rows is an honest ok/empty, not a failure */
        <p className="mt-4 text-sm" style={{ color: "var(--sub)" }}
          data-testid="news-empty">
          0 headlines in window — neutral (no news is neutral news)
        </p>
      ) : (
        <ul className="mt-3 flex flex-col">
          {items.map((it) => (
            <li key={`${it.url}-${it.published_at}`}
              className="border-t py-3 first:border-t-0"
              style={{ borderColor: "var(--line-2)" }}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <a href={it.url} target="_blank" rel="noopener noreferrer"
                  className="text-sm font-semibold underline-offset-2 hover:underline">
                  {it.headline}
                </a>
                <SentimentTag value={it.sentiment} />
              </div>
              <div className="mt-1 text-[10.5px] num" style={{ color: "var(--sub)" }}>
                {it.source_name} · {it.published_at}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Sentiment tag by SIGN of the per-item score: >0 positive, <0 negative,
 * 0/null neutral — always icon+word, never color alone. */
function SentimentTag({ value }: { value: number | null }) {
  const kind = value == null || value === 0 ? "neutral" : value > 0 ? "positive" : "negative";
  const style =
    kind === "positive"
      ? { background: "var(--conf-hi-bg)", color: "var(--conf-hi-ink)" }
      : kind === "negative"
        ? { background: "var(--red-bg)", color: "var(--red)" }
        : { background: "var(--line-2)", color: "var(--sub)" };
  return (
    <span className="num shrink-0 rounded px-1.5 py-0.5 text-[10.5px] font-semibold" style={style}>
      {kind}
      {value != null && ` ${fmtSigned(value)}`}
    </span>
  );
}
