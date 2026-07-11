"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, setToken, type TokenResponse } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(path: "/auth/login" | "/auth/register") {
    setBusy(true);
    setError(null);
    try {
      const response = await api<TokenResponse>(path, {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      setToken(response.access_token);
      router.push("/documents");
    } catch (err) {
      setError(err instanceof Error ? err.message : "something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="w-full max-w-sm rounded-2xl border border-slate-200 bg-white p-8 shadow-sm">
        <h1 className="text-2xl font-semibold">Lexa</h1>
        <p className="mt-1 text-sm text-slate-500">
          Upload large PDFs. Ask questions. Get page-cited answers.
        </p>
        <form
          className="mt-6 space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            void submit("/auth/login");
          }}
        >
          <input
            data-testid="email"
            type="email"
            required
            placeholder="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
          />
          <input
            data-testid="password"
            type="password"
            required
            minLength={8}
            placeholder="password (min 8 chars)"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-500"
          />
          {error && <p className="text-sm text-red-600">{error}</p>}
          <div className="flex gap-2">
            <button
              data-testid="login"
              type="submit"
              disabled={busy}
              className="flex-1 rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
            >
              Log in
            </button>
            <button
              data-testid="register"
              type="button"
              disabled={busy}
              onClick={() => void submit("/auth/register")}
              className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium hover:bg-slate-100 disabled:opacity-50"
            >
              Register
            </button>
          </div>
        </form>
      </div>
    </main>
  );
}
