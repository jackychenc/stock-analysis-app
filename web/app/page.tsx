/**
 * Foundation skeleton page. The real dashboard (A4's S2 spec: CallHeroCard,
 * LensCards, degraded states) lands in roadmap Step 8 (task #14).
 */
export default function Home() {
  return (
    <div className="flex flex-col items-center gap-4 pt-24 text-center">
      <h1 className="text-2xl font-semibold">Stock Investment Analysis</h1>
      <p className="text-slate-600">
        Five lenses, one explainable daily call. Search a ticker to begin.
      </p>
      <p className="rounded-md bg-amber-50 px-4 py-2 text-sm text-amber-700">
        Foundation build — dashboard UI arrives with roadmap Step 8.
      </p>
    </div>
  );
}
