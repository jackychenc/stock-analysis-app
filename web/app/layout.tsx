import type { Metadata } from "next";
import "./globals.css";
import AppNav from "@/components/AppNav";
import DisclaimerBar from "@/components/DisclaimerBar";

export const metadata: Metadata = {
  title: "Stock Investment Analysis",
  description: "Five-lens decision support. Not investment advice.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        {/* task #14: primary tabs (hidden on the login route) */}
        <AppNav />
        {/* bottom padding clears the fixed FR-39 bar (taller when text wraps
            on mobile) so the last card is never occluded (A6 m02 finding) */}
        <main className="mx-auto max-w-5xl p-6 pb-44 sm:pb-28">{children}</main>
        {/* FR-39: persistent, non-hideable on every screen */}
        <DisclaimerBar />
      </body>
    </html>
  );
}
