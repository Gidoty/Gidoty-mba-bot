import { useState } from "react";

import { api } from "../../api/client.js";
import BinanceGuide from "../../components/BinanceGuide.jsx";
import { FormField, inputClass, primaryButton } from "../../components/form.jsx";
import { Card, ErrorBanner, Notice, PageHeader } from "../../components/ui.jsx";

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
    <div className="grid gap-4">
      <PageHeader
        title="Connect Your Binance Account"
        subtitle="Connect your Binance account to allow the Metabridge Trading Bot to execute supported trades on your behalf. Your trading funds remain in your Binance account - they are never transferred to or held by this bot."
      />

      <Notice>
        <strong className="font-semibold">Trading can lose money.</strong> Only use funds you can afford to lose.
      </Notice>

      <BinanceGuide />
      <Steps current={stepIndex} />
      <ErrorBanner message={error} />
      {testResult && !testResult.ok && (
        <p role="alert" className="rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm text-fg">
          {testResult.message}
        </p>
      )}

      {step === "intro" && (
        <Card>
          <p className="text-sm text-fg">
            You'll need an API key from your Binance account with trading permission enabled and{" "}
            <strong>withdrawals switched off</strong>. The bot can never move your funds out of Binance - that's
            enforced by the permissions you grant it, not just by this app.
          </p>
          <button type="button" onClick={() => setStepIndex(1)} className={`mt-4 w-full ${primaryButton}`}>
            Get Started
          </button>
        </Card>
      )}

      {step === "key" && (
        <Card>
          <FormField label="Binance API key">
            <input value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoFocus className={`${inputClass} font-mono`} />
          </FormField>
          <button type="button" onClick={() => setStepIndex(2)} disabled={!apiKey.trim()} className={`mt-4 w-full ${primaryButton}`}>
            Continue
          </button>
        </Card>
      )}

      {step === "secret" && (
        <Card>
          <FormField label="Binance API secret" hint="Shown by Binance only once, right next to your API key when you create it.">
            <input type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} autoFocus className={`${inputClass} font-mono`} />
          </FormField>
          <button type="button" onClick={connectAndVerify} disabled={!apiSecret.trim() || submitting} className={`mt-4 w-full ${primaryButton}`}>
            Verify Connection
          </button>
        </Card>
      )}

      {step === "verifying" && (
        <Card className="flex flex-col items-center py-8 text-center">
          <div role="status" aria-label="Verifying" className="h-8 w-8 animate-spin rounded-full border-4 border-line border-t-brand-line" />
          <p className="mt-3 text-sm text-muted">Verifying your connection with Binance...</p>
        </Card>
      )}

      {step === "success" && (
        <Card className="flex flex-col items-center py-8 text-center">
          <div aria-hidden="true" className="flex h-14 w-14 items-center justify-center rounded-full bg-pos-soft text-2xl text-pos">
            ✓
          </div>
          <p className="mt-3 text-base font-semibold text-fg">Connection Successful</p>
          <p className="mt-1 text-sm text-muted">Your Binance account is now connected.</p>
          <button type="button" onClick={onAccountChanged} className={`mt-4 ${primaryButton} px-8`}>
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
    <ol className="flex items-center gap-1" aria-label="Progress">
      {labels.map((label, i) => (
        <li key={label} className="flex flex-1 items-center last:flex-none" aria-current={i === current ? "step" : undefined}>
          <span
            className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
              i <= current ? "bg-brand text-brand-ink" : "bg-surface-2 text-muted"
            }`}
          >
            {i + 1}
          </span>
          <span className="sr-only">{label}</span>
          {i < labels.length - 1 && <span className={`mx-1 h-0.5 flex-1 ${i < current ? "bg-brand-line" : "bg-line"}`} />}
        </li>
      ))}
    </ol>
  );
}
