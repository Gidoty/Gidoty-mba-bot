import { useState } from "react";

import { api } from "../api/client.js";
import AuthLayout from "../components/AuthLayout.jsx";
import { ErrorBanner } from "../components/ui.jsx";
import { navigate } from "../useHashRoute.js";
import { Field } from "./Signup.jsx";

// The split layout lives in AuthLayout (shared with Signup).
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
    <AuthLayout
      heading="Set it up once. Let the bot watch the market for you."
      blurb="Metabridge Bot follows the market around the clock, so you can spend your time on everything else."
    >
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
    </AuthLayout>
  );
}
