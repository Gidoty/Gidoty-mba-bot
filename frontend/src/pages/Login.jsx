import { useState } from "react";

import { api } from "../api/client.js";
import BrandLogo from "../components/BrandLogo.jsx";
import { ErrorBanner, ThemeSwitch } from "../components/ui.jsx";
import { navigate } from "../useHashRoute.js";
import { Field } from "./Signup.jsx";

// Split screen from 900 px: brand panel on the left, the form on the right. On a phone the
// brand panel becomes a short header. The panel's pattern is the brand mark, not a chart,
// so nothing on this screen suggests a result.
export default function Login({ onAuthed }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const customer = await api.post("/auth/login", { email, password });
      onAuthed(customer);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="min-h-screen bg-app text-fg shell:grid shell:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
      <section className="flex flex-col justify-between gap-8 bg-rail px-4 py-6 shell:px-10 shell:py-10">
        <button type="button" onClick={() => navigate("landing")} className="w-fit text-left" aria-label="Metabridge Bot home">
          <BrandLogo />
        </button>
        <div>
          <h2 className="max-w-[18ch] text-2xl font-semibold leading-tight tracking-tight text-white shell:text-3xl">
            Your Binance key, your risk limit, your stops.
          </h2>
          <p className="mt-3 max-w-[44ch] text-sm text-rail-text">
            The bot works with a trade-only Binance key that you create. It cannot withdraw. Live trades use a stop-loss placed at Binance.
          </p>
        </div>
        <svg viewBox="0 0 400 120" aria-hidden="true" className="hidden h-auto max-h-44 w-full shell:block">
          <g fill="none" stroke="#253352" strokeWidth="2">
            <path d="M0 110c40-70 80-70 120 0s80 70 120 0 80-70 120 0 40 35 40 35" />
            <path d="M0 80c40-70 80-70 120 0s80 70 120 0 80-70 120 0 40 35 40 35" opacity="0.6" />
            <path d="M0 50c40-70 80-70 120 0s80 70 120 0 80-70 120 0 40 35 40 35" opacity="0.3" />
          </g>
        </svg>
      </section>

      <section className="relative flex items-center justify-center px-4 pb-24 pt-10 shell:px-10 shell:pb-10">
        <div className="absolute right-4 top-4 shell:right-6 shell:top-6">
          <ThemeSwitch />
        </div>
        <div className="mt-8 w-full max-w-sm space-y-4 shell:mt-0">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Log in</h1>
            <p className="mt-1 text-sm text-muted">Welcome back.</p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4 rounded-card border border-line bg-surface p-5">
            <ErrorBanner message={error} />
            <Field label="Email" type="email" value={email} onChange={setEmail} autoComplete="username" placeholder="you@example.com" required />
            <div>
              <Field label="Password" type={showPassword ? "text" : "password"} value={password} onChange={setPassword} autoComplete="current-password" required />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-pressed={showPassword}
                className="mt-1.5 text-xs font-medium text-brand-text hover:underline"
              >
                {showPassword ? "Hide password" : "Show password"}
              </button>
            </div>
            <button
              type="submit"
              disabled={submitting}
              className="min-h-[44px] w-full rounded-ctl bg-brand px-4 font-semibold text-brand-ink hover:brightness-105 disabled:opacity-60"
            >
              {submitting ? "Logging in..." : "Log in"}
            </button>
            <p className="text-center text-sm text-muted">
              No account yet?{" "}
              <button type="button" onClick={() => navigate("signup")} className="font-medium text-brand-text hover:underline">
                Sign up
              </button>
            </p>
          </form>
        </div>
      </section>
    </div>
  );
}
