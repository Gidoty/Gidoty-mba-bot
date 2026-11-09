import { useState } from "react";

import { api } from "../../api/client.js";
import HelpTip from "../../components/HelpTip.jsx";
import { usd } from "../../lib/format.js";
import { FormField, inputClass, primaryButton } from "../../components/form.jsx";
import { Card, CardTitle, ErrorBanner, PageHeader, Pill } from "../../components/ui.jsx";

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
    <div className="grid gap-4">
      <PageHeader
        title="Start Your Bot"
        subtitle="Live trading is authorized on this account. The bot will not place a single order until you configure your trading capital and explicitly start it below."
      />

      <Card className={capitalConfigured ? "border-l-4 border-l-pos" : "border-l-4 border-l-warn"}>
        <CardTitle
          right={
            <span className="flex items-center gap-2">
              <Pill tone={capitalConfigured ? "ok" : "warn"}>{capitalConfigured ? "Done" : "To do"}</Pill>
              <HelpTip text="The maximum amount of your Binance balance the bot may use as its sizing base. Your funds stay in your own Binance account - this is a ceiling, not a transfer. The bot will never use more than what Binance actually shows as available, even if you set this higher." />
            </span>
          }
        >
          Step 1: Trading capital
        </CardTitle>
        <p className="text-sm text-muted">
          {capitalConfigured
            ? `Set to ${usd(account.allocated_capital)}. You can change this at any time.`
            : "Tell the bot the maximum amount it may use from your Binance balance."}
        </p>
        <div className="mt-3">
          <ErrorBanner message={capitalError} />
        </div>
        <form onSubmit={saveCapital} className="flex flex-wrap items-end gap-3">
          <FormField label="Amount (US dollars)" className="w-48">
            <div className="relative">
              <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted">$</span>
              <input type="number" min="0.01" step="0.01" value={capitalInput} onChange={(e) => setCapitalInput(e.target.value)} className={`${inputClass} pl-7`} />
            </div>
          </FormField>
          <button type="submit" disabled={savingCapital} className={primaryButton}>
            {savingCapital ? "Saving..." : capitalConfigured ? "Update" : "Save"}
          </button>
        </form>
      </Card>

      <Card>
        <CardTitle right={<Pill>{capitalConfigured ? "Ready" : "Waiting for step 1"}</Pill>}>Step 2: Start the bot</CardTitle>
        <p className="text-sm text-muted">
          Once started, the bot runs continuously on our servers - you don't need to keep this page open, stay
          logged in, or do anything after a trade closes. There is no Pause; the bot keeps scanning for qualifying
          trades until you tell it otherwise.
        </p>
        <div className="mt-3">
          <ErrorBanner message={startError} />
        </div>
        <button type="button" onClick={startBot} disabled={starting || !capitalConfigured} className={primaryButton}>
          {starting ? "Starting..." : "Start Bot"}
        </button>
        {!capitalConfigured && <p className="mt-2 text-xs text-muted">Configure your trading capital above first.</p>}
      </Card>
    </div>
  );
}
