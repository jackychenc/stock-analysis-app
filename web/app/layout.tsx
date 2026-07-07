import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Stock Investment Analysis",
  description: "Five-lens decision support. Not financial advice.",
};

// A4 pattern: concise always-visible line; full canonical FR-39 text on tap
// (full text ships with the dashboard UI in roadmap Step 8).
const DISCLAIMER =
  "For personal decision-support & educational use only — not investment " +
  "advice; model outputs, not from a registered adviser. Full disclaimer ›";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <main className="mx-auto max-w-5xl p-6 pb-16">{children}</main>
        {/* FR-39: persistent disclaimer bar on every screen */}
        <footer className="fixed inset-x-0 bottom-0 border-t border-slate-200 bg-white/95 px-4 py-2 text-center text-xs text-slate-500">
          {DISCLAIMER}
        </footer>
      </body>
    </html>
  );
}
