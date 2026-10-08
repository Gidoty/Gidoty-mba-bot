import { useState } from "react";

import { api } from "../api/client.js";
import { navigate } from "../useHashRoute.js";
import { Field } from "./Signup.jsx";

export default function Login({ onAuthed }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
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
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <form onSubmit={handleSubmit} className="w-full max-w-sm space-y-4 rounded-lg border border-slate-200 bg-white p-8 shadow-sm">
        <h1 className="text-xl font-semibold text-ink">Log in</h1>
        {error && <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
        <Field label="Email" type="email" value={email} onChange={setEmail} required />
        <Field label="Password" type="password" value={password} onChange={setPassword} required />
        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-md bg-accent px-4 py-2 font-medium text-white hover:bg-accent-dark disabled:opacity-60"
        >
          {submitting ? "Logging in..." : "Log in"}
        </button>
        <p className="text-center text-sm text-slate-500">
          No account yet?{" "}
          <button type="button" onClick={() => navigate("signup")} className="text-accent hover:underline">
            Sign up
          </button>
        </p>
      </form>
    </div>
  );
}
