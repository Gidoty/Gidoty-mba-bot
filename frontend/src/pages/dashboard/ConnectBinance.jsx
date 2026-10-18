import { useState } from "react";

import { api } from "../../api/client.js";
import { Card, ErrorBanner } from "../../components/ui.jsx";

const STEPS = ["intro", "key", "secret", "verifying", "success"];

export default function ConnectBinance({ onAccountChanged }) {
  const [stepIndex, setStepIndex] = useState(0);
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [error, setError] = useState(null);
  const [testResult, setTestResult] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const step = STEPS[stepIndex];

  async function connectAndVerify() {
    setError(null);
    setSubmitting(true);
    setStepIndex(3); // "verifying"
    try {
      await api.put("/account/credentials", { api_key: apiKey, api_secret: apiSecret, environment: "mainnet" });
      const result = await api.post("/account/credentials/test");
      setTestResult(result);
      if (result.ok) {
        setStepIndex(4);
      } else {
        setStepIndex(2); // back to the secret step so they can correct it
      }
    } catch (err) {
      setError(err.message);
      setStepIndex(2);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-xl font-semibold text-ink">Connect Your Binance Account</h1>
        <p className="mt-1 text-sm text-slate-600">
          Connect your Binance account to allow the Metabridge Trading Bot to execute supported trades on your
          behalf. Your trading funds remain in your Binance account - they are never transferred to or held by this
          bot.
        </p>
      </div>

      <Steps current={stepIndex} />
      <ErrorBanner message={error} />
      {testResult && !testResult.ok && (
        <p className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{testResult.message}</p>
      )}

      {step === "intro" && (
        <Card>
          <p className="text-sm text-slate-600">
            You'll need an API key from your Binance account with trading permission enabled and{" "}
            <strong>withdrawals switched off</strong>. The bot can never move your funds out of Binance - that's
            enforced by the permissions you grant it, not just by this app.
          </p>
          <HowToGetKey />
          <button
            onClick={() => setStepIndex(1)}
            className="mt-4 w-full rounded-md bg-accent px-4 py-2.5 text-sm font-semibold text-white hover:bg-accent-dark"
          >
            Get Started
          </button>
        </Card>
      )}

      {step === "key" && (
        <Card>
          <label className="block">
            <span className="text-sm font-medium text-slate-700">Binance API key</span>
            <input
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoFocus
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm font-mono"
            />
          </label>
          <button
            onClick={() => setStepIndex(2)}
            disabled={!apiKey.trim()}
            className="mt-4 w-full rounded-md bg-accent px-4 py-2.5 text-sm font-semibold text-white hover:bg-accent-dark disabled:cursor-not-allowed disabled:opacity-40"
          >
            Continue
          </button>
        </Card>
      )}

      {step === "secret" && (
        <Card>
          <label className="block">
            <span className="text-sm font-medium text-slate-700">Binance API secret</span>
            <input
              type="password"
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
              autoFocus
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm font-mono"
            />
          </label>
          <p className="mt-2 text-xs text-slate-500">
            Shown by Binance only once, right next to your API key when you create it.
          </p>
          <button
            onClick={connectAndVerify}
            disabled={!apiSecret.trim() || submitting}
            className="mt-4 w-full rounded-md bg-accent px-4 py-2.5 text-sm font-semibold text-white hover:bg-accent-dark disabled:cursor-not-allowed disabled:opacity-40"
          >
            Verify Connection
          </button>
        </Card>
      )}

      {step === "verifying" && (
        <Card className="flex flex-col items-center py-8 text-center">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-slate-200 border-t-accent" />
          <p className="mt-3 text-sm text-slate-600">Verifying your connection with Binance...</p>
        </Card>
      )}

      {step === "success" && (
        <Card className="flex flex-col items-center py-8 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-accent/10 text-xl text-accent-dark">
            ✓
          </div>
          <p className="mt-3 text-sm font-semibold text-ink">Connection Successful</p>
          <p className="mt-1 text-sm text-slate-600">Your Binance account is now connected.</p>
          <button
            onClick={onAccountChanged}
            className="mt-4 rounded-md bg-accent px-6 py-2.5 text-sm font-semibold text-white hover:bg-accent-dark"
          >
            Continue
          </button>
        </Card>
      )}
    </div>
  );
}

function Steps({ current }) {
  const labels = ["Connect", "API Key", "Secret", "Verify", "Done"];
  return (
    <div className="mb-4 flex items-center gap-1">
      {labels.map((label, i) => (
        <div key={label} className="flex flex-1 items-center">
          <span
            className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ${
              i <= current ? "bg-accent text-white" : "bg-slate-200 text-slate-500"
            }`}
          >
            {i + 1}
          </span>
          {i < labels.length - 1 && <span className={`mx-1 h-0.5 flex-1 ${i < current ? "bg-accent" : "bg-slate-200"}`} />}
        </div>
      ))}
    </div>
  );
}

function HowToGetKey() {
  return (
    <details className="mt-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm">
      <summary className="cursor-pointer font-medium text-ink">Where do I find this on Binance?</summary>
      <ol className="mt-3 list-decimal space-y-2 pl-5 text-slate-600">
        <li>Log in at binance.com (or open the Binance app).</li>
        <li>
          Open your profile icon, then <strong>API Management</strong>.
        </li>
        <li>
          Click <strong>Create API</strong> and give it a label, e.g. "Metabridge Bot". Verify with your password
          and/or 2FA.
        </li>
        <li>
          Click <strong>Edit restrictions</strong> on the new key. Turn on trading for the market you use.{" "}
          <strong className="text-red-700">Leave withdrawals switched off.</strong>
        </li>
        <li>
          Binance shows the API key and secret once - copy both immediately into the fields on the next steps.
        </li>
      </ol>
    </details>
  );
}
