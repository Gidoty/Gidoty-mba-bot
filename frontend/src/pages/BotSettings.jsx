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
  const [pairSelectionMode, setPairSelectionMode] = useState(account.pair_selection_mode);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [capitalInput, setCapitalInput] = useState(account.allocated_capital != null ? String(account.allocated_capital) : "");
  const [savingCapital, setSavingCapital] = useState(false);
  const [capitalError, setCapitalError] = useState(null);

  const dirty =
    pair !== account.pair ||
    marketType !== account.market_type ||
    riskPct !== String(account.risk_pct) ||
    pairSelectionMode !== account.pair_selection_mode;

  async function save(e) {
    e.preventDefault();
    setError(null);
    setSaving(true);
    try {
      await api.patch("/account", {
        pair,
        market_type: marketType,
        risk_pct: riskPct ? Number(riskPct) : undefined,
        pair_selection_mode: pairSelectionMode,
      });
      await refreshAccount();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function saveCapital(e) {
    e.preventDefault();
    setCapitalError(null);
    const value = Number(capitalInput);
    if (!capitalInput || !(value > 0)) {
      setCapitalError("Enter a trading capital amount greater than 0.");
      return;
    }
    setSavingCapital(true);
    try {
      await api.patch("/account", { allocated_capital: value });
      await refreshAccount();
    } catch (err) {
      setCapitalError(err.message);
    } finally {
      setSavingCapital(false);
    }
  }

  return (
    <div>
      <PageHeader title="Bot Settings" subtitle="How your bot is configured." />
      <ErrorBanner message={error} />

      <Card className="mb-4">
        <div className="flex items-center gap-1">
          <p className="text-sm font-medium text-ink">Trading Capital</p>
          <HelpTip text="The maximum amount of your Binance balance the bot may use as its sizing base. Your funds stay in your own Binance account at all times - this is a ceiling, not a transfer. The bot will never use more than what Binance actually shows as available, even if this is set higher." />
        </div>
        <p className="mt-1 text-xs text-slate-500">
          {account.allocated_capital != null
            ? `Currently set to ${account.allocated_capital}. You can raise or lower it at any time, even with a trade open - it only affects your bot's next new trade.`
            : "Not configured yet. Required before you can start the bot on a live account."}
        </p>
        <ErrorBanner message={capitalError} />
        <form onSubmit={saveCapital} className="mt-3 flex items-end gap-2">
          <label className="block">
            <span className="text-xs font-medium text-slate-600">Amount</span>
            <input
              type="number"
              min="0.01"
              step="0.01"
              value={capitalInput}
              onChange={(e) => setCapitalInput(e.target.value)}
              className="mt-1 w-40 rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </label>
          <button
            type="submit"
            disabled={savingCapital}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-dark disabled:cursor-not-allowed disabled:opacity-40"
          >
            {savingCapital ? "Saving..." : "Save"}
          </button>
        </form>
      </Card>

      <form onSubmit={save} className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="flex items-center gap-1">
          <h2 className="text-sm font-medium text-ink">Trading settings</h2>
          <HelpTip text="You can change your pair, market, risk, or pair selection mode at any time. Switching any of them is blocked while a trade is open, so nothing gets left unmanaged." />
        </div>

        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-4">
          <label className="block">
            <span className="text-xs font-medium text-slate-600">Pair selection</span>
            <select
              value={pairSelectionMode}
              onChange={(e) => setPairSelectionMode(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              <option value="manual">Manual</option>
              <option value="auto">Auto Select</option>
            </select>
          </label>

          <label className="block">
            <span className="text-xs font-medium text-slate-600">Pair{pairSelectionMode === "auto" ? " (Auto Select active)" : ""}</span>
            <select
              value={pair}
              onChange={(e) => setPair(e.target.value)}
              disabled={pairSelectionMode === "auto"}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-50 disabled:text-slate-400"
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
