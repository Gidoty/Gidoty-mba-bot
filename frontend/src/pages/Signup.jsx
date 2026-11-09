import { useId, useState } from "react";

import { api } from "../api/client.js";
import BrandLogo from "../components/BrandLogo.jsx";
import ConsentCheckbox from "../components/ConsentCheckbox.jsx";
import { navigate } from "../useHashRoute.js";

export default function Signup({ onAuthed }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [acceptTerms, setAcceptTerms] = useState(false);
  const [acknowledgePrivacy, setAcknowledgePrivacy] = useState(false);
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
    <div className="flex min-h-screen flex-col items-center justify-center bg-slate-50 px-4 py-10">
      <div className="mb-6">
        <BrandLogo dark={false} subtitle={null} />
      </div>
      <form onSubmit={handleSubmit} className="w-full max-w-sm space-y-4 rounded-lg border border-slate-200 bg-white p-8 shadow-sm">
        <h1 className="text-xl font-semibold text-ink">Create your account</h1>
        {error && <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
        <Field label="Email" type="email" value={email} onChange={setEmail} required />
        <Field label="Password" type="password" value={password} onChange={setPassword} minLength={8} required />

        <ConsentCheckbox
          checked={acceptTerms}
          onChange={setAcceptTerms}
          label={
            <>
              I accept the{" "}
              <a href="#/terms" target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
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
              <a href="#/privacy" target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
                Privacy Policy
              </a>
            </>
          }
        />

        <button
          type="submit"
          disabled={submitting || !acceptTerms || !acknowledgePrivacy}
          className="w-full rounded-md bg-accent px-4 py-2 font-medium text-white hover:bg-accent-dark disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitting ? "Creating account..." : "Sign up"}
        </button>
        <p className="text-center text-sm text-slate-500">
          Already have an account?{" "}
          <button type="button" onClick={() => navigate("login")} className="text-accent hover:underline">
            Log in
          </button>
        </p>
      </form>
    </div>
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
