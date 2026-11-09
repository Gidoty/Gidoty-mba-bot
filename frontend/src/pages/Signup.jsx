import { useId, useState } from "react";

import { api } from "../api/client.js";
import AuthLayout from "../components/AuthLayout.jsx";
import ConsentCheckbox from "../components/ConsentCheckbox.jsx";
import { ErrorBanner } from "../components/ui.jsx";
import { navigate } from "../useHashRoute.js";

export default function Signup({ onAuthed }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [acceptTerms, setAcceptTerms] = useState(false);
  const [acknowledgePrivacy, setAcknowledgePrivacy] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const customer = await api.post("/auth/signup", {
        email,
        password,
        accept_terms: acceptTerms,
        acknowledge_privacy: acknowledgePrivacy,
      });
      onAuthed(customer);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthLayout
      heading="Start in practice mode. No Binance account, no card."
      blurb="Pick your pair, link Telegram and watch the bot work with simulated trades. Connecting a real Binance account is a separate step, later."
    >
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Create your account</h1>
        <p className="mt-1 text-sm text-muted">It takes a minute.</p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4 rounded-card border border-line bg-surface p-5">
        <ErrorBanner message={error} />
        <Field label="Email" type="email" value={email} onChange={setEmail} autoComplete="email" placeholder="you@example.com" required />
        <div>
          <Field label="Password" type={showPassword ? "text" : "password"} value={password} onChange={setPassword} autoComplete="new-password" minLength={8} required />
          <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs text-muted">At least 8 characters.</span>
            <button type="button" onClick={() => setShowPassword((v) => !v)} aria-pressed={showPassword} className="text-xs font-medium text-brand-text hover:underline">
              {showPassword ? "Hide password" : "Show password"}
            </button>
          </div>
        </div>

        <div className="space-y-3">
          <ConsentCheckbox
            checked={acceptTerms}
            onChange={setAcceptTerms}
            label={
              <>
                I accept the{" "}
                <a href="#/terms" target="_blank" rel="noopener noreferrer" className="font-medium text-brand-text hover:underline">
                  Terms of Service
                </a>
              </>
            }
          />
          <ConsentCheckbox
            checked={acknowledgePrivacy}
            onChange={setAcknowledgePrivacy}
            label={
              <>
                I acknowledge the{" "}
                <a href="#/privacy" target="_blank" rel="noopener noreferrer" className="font-medium text-brand-text hover:underline">
                  Privacy Policy
                </a>
              </>
            }
          />
        </div>

        <button
          type="submit"
          disabled={submitting || !acceptTerms || !acknowledgePrivacy}
          className="min-h-[44px] w-full rounded-ctl bg-brand px-4 font-semibold text-brand-ink hover:brightness-105 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitting ? "Creating account..." : "Sign up"}
        </button>
        <p className="text-center text-sm text-muted">
          Already have an account?{" "}
          <button type="button" onClick={() => navigate("login")} className="font-medium text-brand-text hover:underline">
            Log in
          </button>
        </p>
      </form>
    </AuthLayout>
  );
}

export function Field({ label, type = "text", value, onChange, id, ...rest }) {
  const auto = useId();
  const inputId = id || auto;
  return (
    <div>
      <label htmlFor={inputId} className="block text-sm font-medium text-fg">
        {label}
      </label>
      <input
        id={inputId}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1.5 min-h-[44px] w-full rounded-ctl border border-line-strong bg-surface px-3.5 text-sm text-fg placeholder:text-muted"
        {...rest}
      />
    </div>
  );
}
