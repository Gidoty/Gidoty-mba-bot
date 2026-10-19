import { useState } from "react";

import { api } from "../api/client.js";
import BinanceGuide from "./BinanceGuide.jsx";
import HelpTip from "./HelpTip.jsx";
import { Badge, Card } from "./ui.jsx";

// The "connected" state of the Binance relationship, plus Manage
// Connection (Connection Status, Test Connection, Replace Credentials).
// Deliberately no Disconnect for this release - replacing with a
// testnet-tagged credential is the only way to step back from live
// trading, and that's done explicitly through Replace Credentials, not
// a separate delete action.
export default function BinanceConnectionPanel({ account, onAccountChanged }) {
  const [managing, setManaging] = useState(false);
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
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-semibold text-ink">Binance</p>
          <p className="mt-0.5 flex items-center gap-1.5 text-sm text-slate-600">
            <span className="h-2 w-2 rounded-full bg-accent" /> Connected
          </p>
        </div>
        <Badge tone="accent">Trading connection active</Badge>
      </div>

      <button
        onClick={() => setManaging((v) => !v)}
        className="mt-3 text-sm font-medium text-accent hover:underline"
      >
        {managing ? "Hide" : "Manage Connection"}
      </button>

      {managing && (
        <div className="mt-4 space-y-4 border-t border-slate-100 pt-4">
          <BinanceGuide />

          <div>
            <p className="text-sm font-medium text-ink">Connection status</p>
            <p className="mt-1 text-sm text-slate-600">
              A mainnet Binance API key is on file for this account.{" "}
              {account.live_trading_enabled_at
                ? "Live trading is currently enabled."
                : "Live trading is not enabled yet."}
            </p>
          </div>

          <div>
            <div className="flex items-center gap-1">
              <p className="text-sm font-medium text-ink">Test Connection</p>
              <HelpTip text="Confirms Binance accepts this stored key, without placing any order." />
            </div>
            <button
              onClick={testConnection}
              disabled={testing}
              className="mt-2 rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-ink hover:bg-slate-50 disabled:opacity-60"
            >
              {testing ? "Testing..." : "Test Connection"}
            </button>
            {testResult && testResult.ok && (
              <p className="mt-2 text-sm text-accent-dark">Connection verified.</p>
            )}
            {testResult && !testResult.ok && (
              <div className="mt-2 rounded-md border border-red-200 bg-red-50 px-3 py-2">
                <p className="text-sm font-medium text-red-800">Binance connection needs attention</p>
                <p className="mt-0.5 text-sm text-red-700">{testResult.message}</p>
              </div>
            )}
          </div>

          <div>
            <button
              onClick={() => setReplacing((v) => !v)}
              className="text-sm font-medium text-accent hover:underline"
            >
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
          </div>
        </div>
      )}
    </Card>
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
    <form onSubmit={save} className="mt-3 space-y-3 rounded-md border border-slate-200 bg-slate-50 p-3">
      {environment !== "mainnet" && (
        <p className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
          Replacing with a testnet key will turn live trading off for this account.
        </p>
      )}
      {error && <p className="rounded-md bg-red-50 px-3 py-2 text-xs text-red-700">{error}</p>}
      <label className="block">
        <span className="text-xs font-medium text-slate-600">New API key</span>
        <input
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          required
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm font-mono"
        />
      </label>
      <label className="block">
        <span className="text-xs font-medium text-slate-600">New API secret</span>
        <input
          type="password"
          value={apiSecret}
          onChange={(e) => setApiSecret(e.target.value)}
          required
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm font-mono"
        />
      </label>
      <label className="block">
        <span className="text-xs font-medium text-slate-600">Environment</span>
        <select
          value={environment}
          onChange={(e) => setEnvironment(e.target.value)}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        >
          <option value="mainnet">Mainnet (real Binance account)</option>
          <option value="testnet">Testnet (practice)</option>
        </select>
        <span className="mt-1 block text-xs text-slate-500">
          Practice keys: Spot uses the Binance Spot Testnet; Futures uses Binance Demo Trading (demo.binance.com).
        </span>
      </label>
      <button
        type="submit"
        disabled={saving || !apiKey || !apiSecret}
        className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-dark disabled:cursor-not-allowed disabled:opacity-40"
      >
        {saving ? "Saving..." : "Save new credentials"}
      </button>
    </form>
  );
}
