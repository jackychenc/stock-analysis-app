"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/** Primary tabs (task #14): Dashboard · Decisions · Settings. Lens-detail and
 * Backtest pages belong to the Dashboard's ticker context, so the Dashboard
 * tab stays active there. Hidden on the login route. */
const TABS = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/decisions", label: "Decisions" },
  { href: "/settings", label: "Settings" },
];

export default function AppNav() {
  const pathname = usePathname();
  if (pathname === "/") return null; // login screen — no app chrome
  return (
    <nav
      aria-label="Primary"
      className="mx-auto flex max-w-5xl items-center gap-1 px-6 pt-4"
    >
      {TABS.map((t) => {
        const active =
          pathname === t.href ||
          pathname.startsWith(`${t.href}/`) ||
          (t.href === "/dashboard" &&
            (pathname.startsWith("/lens") || pathname.startsWith("/backtest")));
        return (
          <Link
            key={t.href}
            href={t.href}
            aria-current={active ? "page" : undefined}
            className="rounded-lg px-3 py-1.5 text-sm font-semibold"
            style={
              active
                ? { background: "var(--card)", color: "var(--ink)", boxShadow: "var(--shadow)" }
                : { color: "var(--sub)" }
            }
          >
            {t.label}
          </Link>
        );
      })}
    </nav>
  );
}
