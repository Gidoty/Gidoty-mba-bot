import { useState } from "react";

import { api } from "../../api/client.js";
import HelpTip from "../../components/HelpTip.jsx";
import { Card, ErrorBanner } from "../../components/ui.jsx";

// Shown once an account has enabled live trading (accepted the risk
// disclosure) but hasn't yet taken the separate, final "Start Bot"
// action - see onboarding.resolve_trading_mode's fifth gate. Enabling
// live trading authorizes going live; it never starts anything by
// itself, so this screen - not the dashboard - is what a fully
// authorized-but-not-started account sees.
export default function StartBot({ account, onAccountChanged }) {
  const [capitalInput, setCapitalInput] = useState(account.allocated_capital != null ? String(account.allocated_capital) : "");
  const [savingCapital, setSavingCapital] = useState(false);
  const [capitalError, setCapitalError] = useState(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState(null);

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
      await onAccountChanged();
    } catch (err) {
      setCapitalError(err.message);
    } finally {
      setSavingCapital(false);
    }
  }

  async function startBot() {
    setStartError(null);
    setStarting(true);
    try {
      await api.post("/account/start-bot", { confirm: true });
      await onAccountChanged();
    } catch (err) {
      setStartError(err.message);
    } finally {
      setStarting(false);
    }
  }

  const capitalConfigured = account.allocated_capital != null;

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-xl font-semibold text-ink">Start Your Bot</h1>
        <p className="mt-1 text-sm text-slate-600">
          Live trading is authorized on this account. The bot will not place a single order until you configure
          your trading capital and explicitly start it below.
        </p>
      </div>

      <Card className={capitalConfigured ? "border-green-200 bg-green-50" : "border-amber-200 bg-amber-50"}>
        <div className="flex items-center gap-1">
          <p className={`text-sm font-semibold ${capitalConfigured ? "text-green-800" : "text-amber-800"}`}>
            1. Trading Capital
          </p>
          <HelpTip text="The maximum amount of your Binance balance the bot may use as its sizing base. Your funds stay in your own Binance account - this is a ceiling, not a transfer. The bot will never use more than what Binance actually shows as available, even if you set this higher." />
        </div>
        <p className="mt-1 text-sm text-slate-600">
          {capitalConfigured
            ? `Set to ${account.allocated_capital}. You can change this at any time.`
            : "Tell the bot the maximum amount it may use from your Binance balance."}
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
            {savingCapital ? "Saving..." : capitalConfigured ? "Update" : "Save"}
          </button>
        </form>
      </Card>

      <Card className="mt-4 border-slate-200">
        <p className="text-sm font-semibold text-ink">2. Start the bot</p>
        <p className="mt-1 text-sm text-slate-600">
          Once started, the bot runs continuously on our servers - you don't need to keep this page open, stay
          logged in, or do anything after a trade closes. There is no Pause; the bot keeps scanning for qualifying
          trades until you tell it otherwise.
        </p>
        <ErrorBanner message={startError} />
        <button
          onClick={startBot}
          disabled={starting || !capitalConfigured}
          className="mt-3 rounded-md bg-accent px-4 py-2.5 text-sm font-semibold text-white hover:bg-accent-dark disabled:cursor-not-allowed disabled:opacity-40"
        >
          {starting ? "Starting..." : "Start Bot"}
        </button>
        {!capitalConfigured && <p className="mt-2 text-xs text-slate-500">Configure your Trading Capital above first.</p>}
      </Card>
    </div>
  );
}
