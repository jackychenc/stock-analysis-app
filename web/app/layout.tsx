import type { Metadata } from "next";
import "./globals.css";
import DisclaimerBar from "@/components/DisclaimerBar";

export const metadata: Metadata = {
  title: "Stock Investment Analysis",
  description: "Five-lens decision support. Not investment advice.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <main className="mx-auto max-w-5xl p-6 pb-24">{children}</main>
        {/* FR-39: persistent, non-hideable on every screen */}
        <DisclaimerBar />
      </body>
    </html>
  );
}
