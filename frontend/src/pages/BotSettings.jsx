import { useState } from "react";

import { api } from "../api/client.js";
import HelpTip from "../components/HelpTip.jsx";
import { Card, ErrorBanner, PageHeader } from "../components/ui.jsx";
import { MARKET_TYPES, PAIRS, UPCOMING_PAIRS } from "../constants.js";
import { useAccount } from "../context/AccountContext.jsx";

export default function BotSettings() {
  const { account, refreshAccount } = useAccount();
  const [pair, setPair] = useState(account.pair);
  const [marketType, setMarketType] = useState(account.market_type);
  const [riskPct, setRiskPct] = useState(String(account.risk_pct));
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [togglingPause, setTogglingPause] = useState(false);

  const dirty = pair !== account.pair || marketType !== account.market_type || riskPct !== String(account.risk_pct);

  async function save(e) {
    e.preventDefault();
    setError(null);
    setSaving(true);
    try {
      await api.patch("/account", { pair, market_type: marketType, risk_pct: riskPct ? Number(riskPct) : undefined });
      await refreshAccount();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function togglePause() {
    setError(null);
    setTogglingPause(true);
    try {
      await api.patch("/account", { is_active: !account.is_active });
      await refreshAccount();
    } catch (err) {
      setError(err.message);
    } finally {
      setTogglingPause(false);
    }
  }

  return (
    <div>
      <PageHeader title="Bot Settings" subtitle="How your bot is configured." />
      <ErrorBanner message={error} />

      <Card className="mb-4 flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-ink">Trading</p>
          <p className="text-xs text-slate-500">{account.is_active ? "The bot can open new trades." : "Paused - no new trades will open."}</p>
        </div>
        <button
          onClick={togglePause}
          disabled={togglingPause}
          className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-ink hover:bg-slate-50 disabled:opacity-60"
        >
          {account.is_active ? "Pause" : "Resume"}
        </button>
      </Card>

      <form onSubmit={save} className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="flex items-center gap-1">
          <h2 className="text-sm font-medium text-ink">Trading settings</h2>
          <HelpTip text="You can change your pair, market, or risk at any time. Switching pair or market is blocked while a trade is open, so nothing gets left unmanaged." />
        </div>

        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="block">
            <span className="text-xs font-medium text-slate-600">Pair</span>
            <select
              value={pair}
              onChange={(e) => setPair(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              {PAIRS.map((p) => (
                <option key={p}>{p}</option>
              ))}
              {UPCOMING_PAIRS.map((p) => (
                <option key={p} value={p} disabled>
                  {p} - coming soon
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="text-xs font-medium text-slate-600">Market</span>
            <select
              value={marketType}
              onChange={(e) => setMarketType(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              {MARKET_TYPES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="text-xs font-medium text-slate-600">Risk per trade (%)</span>
            <input
              type="number"
              min="0.1"
              max="2"
              step="0.1"
              value={riskPct}
              onChange={(e) => setRiskPct(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </label>
        </div>

        <button
          type="submit"
          disabled={!dirty || saving}
          className="mt-4 rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-dark disabled:cursor-not-allowed disabled:opacity-40"
        >
          {saving ? "Saving..." : "Save changes"}
        </button>
      </form>
    </div>
  );
}
