import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import AuthLayout from "../components/AuthLayout.jsx";
import { ErrorBanner } from "../components/ui.jsx";
import { emailProblem, newPasswordProblem, tokenFromRoute } from "../lib/recovery.js";
import { navigate } from "../useHashRoute.js";
import { Field } from "./Signup.jsx";

const button = "min-h-[44px] w-full rounded-ctl bg-brand px-4 font-semibold text-brand-ink hover:brightness-105 disabled:opacity-60";

function Shell({ title, children }) {
  return (
    <AuthLayout heading="Metabridge Bot" blurb="Account recovery.">
      <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
      <div className="space-y-4 rounded-card border border-line bg-surface p-5">{children}</div>
    </AuthLayout>
  );
}

// Ask for a reset link. The answer is the same for every address; when the service cannot send email the server says so and this screen shows it.
export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState(null);
  const [done, setDone] = useState(null);
  async function submit(e) {
    e.preventDefault();
    const problem = emailProblem(email);
    if (problem) return setError(problem);
    setError(null);
    try {
      setDone((await api.post("/auth/password-reset/request", { email: email.trim() })).message);
    } catch (err) {
      setError(err.message);
    }
  }
  return (
    <Shell title="Forgot your password?">
      {done ? <p className="text-sm text-fg" role="status">{done}</p> : (
        <form onSubmit={submit} className="space-y-4">
          <ErrorBanner message={error} />
          <Field label="Email" type="email" value={email} onChange={setEmail} autoComplete="username" required />
          <button type="submit" className={button}>Send the link</button>
        </form>
      )}
      <button type="button" onClick={() => navigate("login")} className="text-sm font-medium text-brand-text hover:underline">Back to log in</button>
    </Shell>
  );
}

export function ResetPassword({ route }) {
  const token = tokenFromRoute(route, "reset-password");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState(null);
  const [done, setDone] = useState(false);
  if (!token) return <Shell title="Choose a new password"><p className="text-sm text-fg">This link is not valid. Ask for a new one.</p><button type="button" onClick={() => navigate("forgot-password")} className="text-sm font-medium text-brand-text hover:underline">Ask for a new link</button></Shell>;
  async function submit(e) {
    e.preventDefault();
    const problem = newPasswordProblem(password, again);
    if (problem) return setError(problem);
    setError(null);
    try {
      await api.post("/auth/password-reset/confirm", { token, new_password: password });
      setDone(true);
    } catch (err) {
      setError(err.message);
    }
  }
  return (
    <Shell title="Choose a new password">
      {done ? (
        <>
          <p className="text-sm text-fg" role="status">Your password has been changed.</p>
          <button type="button" onClick={() => navigate("login")} className={button}>Log in</button>
        </>
      ) : (
        <form onSubmit={submit} className="space-y-4">
          <ErrorBanner message={error} />
          <Field label="New password" type="password" value={password} onChange={setPassword} autoComplete="new-password" minLength={8} required />
          <Field label="New password again" type="password" value={again} onChange={setAgain} autoComplete="new-password" minLength={8} required />
          <button type="submit" className={button}>Change password</button>
        </form>
      )}
    </Shell>
  );
}

export function VerifyEmail({ route }) {
  const token = tokenFromRoute(route, "verify-email");
  const [state, setState] = useState(token ? "working" : "bad");
  useEffect(() => {
    if (!token) return;
    api.post("/auth/email/verify/confirm", { token }).then(() => setState("ok"), () => setState("bad"));
  }, [token]);
  return (
    <Shell title="Confirm your email address">
      <p className="text-sm text-fg" role="status">
        {state === "working" ? "Confirming..." : state === "ok" ? "Your email address is confirmed." : "This link is not valid or has expired. Ask for a new one from your Account page."}
      </p>
      <button type="button" onClick={() => navigate("login")} className="text-sm font-medium text-brand-text hover:underline">Go to the app</button>
    </Shell>
  );
}
