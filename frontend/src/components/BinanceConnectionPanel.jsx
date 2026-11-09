import { useState } from "react";

import { api } from "../api/client.js";
import BinanceGuide from "./BinanceGuide.jsx";
import { FormField, inputClass, linkButton, primaryButton, secondaryButton } from "./form.jsx";
import HelpTip from "./HelpTip.jsx";
import { Card, CardTitle, Notice, Pill } from "./ui.jsx";

const ENVIRONMENT = {
  mainnet: "Real Binance account",
  testnet: "Practice (testnet)",
  unknown: "Unknown",
};

// The "connected" state of the Binance relationship, plus Manage
// Connection (Connection Status, Test Connection, Replace Credentials).
// Deliberately no Disconnect for this release - replacing with a
// testnet-tagged credential is the only way to step back from live
// trading, and that's done explicitly through Replace Credentials, not
// a separate delete action. Only the look changed in the redesign: the
// requests, the fields and what is sent are exactly as before.
export default function BinanceConnectionPanel({ account, onAccountChanged, defaultOpen = false }) {
  const [managing, setManaging] = useState(defaultOpen);
  const [testResult, setTestResult] = useState(null);
  const [testing, setTesting] = useState(false);
  const [replacing, setReplacing] = useState(false);

  async function testConnection() {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api.post("/account/credentials/test");
      setTestResult(result);
    } catch (err) {
      setTestResult({ ok: false, message: err.message });
    } finally {
      setTesting(false);
    }
  }

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-lg font-semibold text-fg">Binance</p>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted">
            <Pill tone="ok">Connected</Pill>
            Trading connection active
          </p>
        </div>
        <button type="button" onClick={() => setManaging((v) => !v)} aria-expanded={managing} className={linkButton}>
          {managing ? "Hide details" : "Manage Connection"}
        </button>
      </div>

      {managing && (
        <div className="mt-5 space-y-6 border-t border-line pt-5">
          <BinanceGuide />

          <section>
            <CardTitle>Connection status</CardTitle>
            <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-3">
              <Item label="API key">A mainnet Binance API key is on file.</Item>
              <Item label="Environment">{ENVIRONMENT[account.credential_environment] || "Not recorded"}</Item>
              <Item label="Live trading">{account.live_trading_enabled_at ? "Enabled" : "Not enabled yet"}</Item>
            </dl>
            {account.credential_blocked && account.credential_block_message && (
              <div className="mt-3">
                <Notice>{account.credential_block_message}</Notice>
              </div>
            )}
          </section>

          <section>
            <CardTitle right={<HelpTip text="Confirms Binance accepts this stored key, without placing any order." />}>Test connection</CardTitle>
            <button type="button" onClick={testConnection} disabled={testing} className={secondaryButton}>
              {testing ? "Testing..." : "Test Connection"}
            </button>
            {testResult && testResult.ok && (
              <p role="status" className="mt-3 flex items-center gap-2 text-sm text-fg">
                <Pill tone="ok">Verified</Pill> Connection verified.
              </p>
            )}
            {testResult && !testResult.ok && (
              <div role="alert" className="mt-3 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3.5 py-3">
                <p className="text-sm font-semibold text-fg">Binance connection needs attention</p>
                <p className="mt-0.5 text-sm text-fg">{testResult.message}</p>
              </div>
            )}
          </section>

          <section>
            <CardTitle>Replace credentials</CardTitle>
            <button type="button" onClick={() => setReplacing((v) => !v)} className={linkButton}>
              {replacing ? "Cancel" : "Replace Credentials"}
            </button>
            {replacing && (
              <ReplaceCredentialsForm
                onDone={() => {
                  setReplacing(false);
                  onAccountChanged();
                }}
              />
            )}
          </section>
        </div>
      )}
    </Card>
  );
}

function Item({ label, children }) {
  return (
    <div className="rounded-ctl border border-line bg-surface-2 px-3.5 py-3">
      <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
      <dd className="mt-1 text-fg">{children}</dd>
    </div>
  );
}

function ReplaceCredentialsForm({ onDone }) {
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [environment, setEnvironment] = useState("mainnet");
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

  async function save(e) {
    e.preventDefault();
    setError(null);
    setSaving(true);
    try {
      await api.put("/account/credentials", { api_key: apiKey, api_secret: apiSecret, environment });
      onDone();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={save} className="mt-3 space-y-4 rounded-ctl border border-line bg-surface-2 p-4">
      {environment !== "mainnet" && <Notice>Replacing with a testnet key will turn live trading off for this account.</Notice>}
      {error && (
        <p role="alert" className="rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm text-fg">
          {error}
        </p>
      )}
      <FormField label="New API key">
        <input value={apiKey} onChange={(e) => setApiKey(e.target.value)} required className={`${inputClass} font-mono`} />
      </FormField>
      <FormField label="New API secret">
        <input type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} required className={`${inputClass} font-mono`} />
      </FormField>
      <FormField
        label="Environment"
        hint="Practice keys: Spot uses the Binance Spot Testnet; Futures uses Binance Demo Trading (demo.binance.com)."
      >
        <select value={environment} onChange={(e) => setEnvironment(e.target.value)} className={inputClass}>
          <option value="mainnet">Mainnet (real Binance account)</option>
          <option value="testnet">Testnet (practice)</option>
        </select>
      </FormField>
      <button type="submit" disabled={saving || !apiKey || !apiSecret} className={primaryButton}>
        {saving ? "Saving..." : "Save new credentials"}
      </button>
    </form>
  );
}
