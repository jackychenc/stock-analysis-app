"use client";

/** Login page (A4 handoff §5) — single personal account, session-cookie flow.
 * On success → Dashboard. */

import { useRouter } from "next/navigation";
import { useState } from "react";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, client: "web" }),
        credentials: "include",
      });
      if (r.ok) {
        router.push("/dashboard");
        return;
      }
      setError(r.status === 401 ? "Incorrect credentials." : `Login failed (${r.status}).`);
    } catch {
      setError("Cannot reach the API — is the local stack running?");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-center pt-20">
      {/* A4 redline: fluid on mobile — never clips at 390px */}
      <div className="card p-6 sm:p-8"
        style={{ borderRadius: "var(--r-hero)", width: "min(100% - 32px, 420px)" }}>
        <h1 className="text-xl font-extrabold">Stock Investment Analysis</h1>
        <p className="mt-1 text-sm" style={{ color: "var(--sub)" }}>
          Five lenses, one explainable daily call.
        </p>
        <form onSubmit={submit} className="mt-6 flex flex-col gap-4">
          <label className="text-sm font-medium">
            Username
            <input
              className="mt-1 w-full rounded-lg border px-3 py-2.5 text-sm outline-none focus:ring-2"
              style={{ borderColor: "var(--line)" }}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              required
            />
          </label>
          <label className="text-sm font-medium">
            Password
            <input
              type="password"
              className="mt-1 w-full rounded-lg border px-3 py-2.5 text-sm outline-none focus:ring-2"
              style={{ borderColor: "var(--line)" }}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </label>
          {error && (
            <p role="alert" className="rounded-lg px-3 py-2 text-sm"
              style={{ background: "var(--red-bg)", color: "var(--red)" }}>
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={busy}
            className="min-h-11 rounded-xl px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60"
            style={{ background: "var(--accent)" }}
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
