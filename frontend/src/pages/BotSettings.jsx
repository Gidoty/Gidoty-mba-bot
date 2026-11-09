import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import HelpTip from "../components/HelpTip.jsx";
import { FormField, SavedNote, inputClass, primaryButton } from "../components/form.jsx";
import { Card, CardTitle, ErrorBanner, PageHeader, Pill, ThemeSwitch } from "../components/ui.jsx";
import { MARKET_TYPES, UPCOMING_PAIRS } from "../constants.js";
import { useAccount } from "../context/AccountContext.jsx";
import { optionLabel, usePairs, withCurrentPair } from "../usePairs.js";

export default function BotSettings() {
  const { account, reloadAccount } = useAccount();
  const [pair, setPair] = useState(account.pair);
  const [marketType, setMarketType] = useState(account.market_type);
  const [riskPct, setRiskPct] = useState(String(account.risk_pct));
  const [pairSelectionMode, setPairSelectionMode] = useState(account.pair_selection_mode);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [capitalSaved, setCapitalSaved] = useState(false);
  const [capitalInput, setCapitalInput] = useState(account.allocated_capital != null ? String(account.allocated_capital) : "");
  const [savingCapital, setSavingCapital] = useState(false);
  const [capitalError, setCapitalError] = useState(null);

  // Selectable pairs for the chosen market, with per-customer availability. Re-read when the
  // saved trading capital changes (availability depends on it).
  const { pairs: pairOptions, loaded: pairsLoaded } = usePairs(marketType, account.allocated_capital);
  const pairList = withCurrentPair(pairOptions, pair);
  // Switching market: a pair that does not exist for the new market is replaced by the first available one.
  useEffect(() => {
    if (pairsLoaded && !pairOptions.some((p) => p.pair === pair)) {
      const next = pairOptions.find((p) => p.available);
      if (next) setPair(next.pair);
    }
  }, [pairsLoaded, pairOptions]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty =
    pair !== account.pair ||
    marketType !== account.market_type ||
    riskPct !== String(account.risk_pct) ||
    pairSelectionMode !== account.pair_selection_mode;

  async function save(e) {
    e.preventDefault();
    setError(null);
    setSaved(false);
    setSaving(true);
    try {
      await api.patch("/account", {
        pair,
        market_type: marketType,
        risk_pct: riskPct ? Number(riskPct) : undefined,
        pair_selection_mode: pairSelectionMode,
      });
      await reloadAccount();
      setSaved(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function saveCapital(e) {
    e.preventDefault();
    setCapitalError(null);
    setCapitalSaved(false);
    const value = Number(capitalInput);
    if (!capitalInput || !(value > 0)) {
      setCapitalError("Enter a trading capital amount greater than 0.");
      return;
    }
    setSavingCapital(true);
    try {
      await api.patch("/account", { allocated_capital: value });
      await reloadAccount();
      setCapitalSaved(true);
    } catch (err) {
      setCapitalError(err.message);
    } finally {
      setSavingCapital(false);
    }
  }

  const marketLabel = MARKET_TYPES.find((m) => m.value === account.market_type)?.label || account.market_type;

  return (
    <div className="grid gap-4">
      <PageHeader title="Bot Settings" subtitle="How your bot is configured.">
        <span className="flex flex-wrap items-center gap-2">
          <Pill tone="info">{account.pair}</Pill>
          <Pill>{marketLabel}</Pill>
          <Pill>{account.risk_pct}% risk per trade</Pill>
        </span>
      </PageHeader>
      <ErrorBanner message={error} />

      <Card>
        <CardTitle>Appearance</CardTitle>
        <p className="mb-3 text-sm text-muted">Auto follows your device. Your choice is saved in this browser.</p>
        <ThemeSwitch />
      </Card>

      <Card>
        <CardTitle right={<HelpTip text="The maximum amount of your Binance balance the bot may use as its sizing base. Your funds stay in your own Binance account at all times - this is a ceiling, not a transfer. The bot will never use more than what Binance actually shows as available, even if this is set higher." />}>
          Trading capital
        </CardTitle>
        <p className="text-sm text-muted">
          {account.allocated_capital != null
            ? `Currently set to ${account.allocated_capital}. You can raise or lower it at any time, even with a trade open - it only affects your bot's next new trade.`
            : "Not configured yet. Required before you can start the bot on a live account."}
        </p>
        <div className="mt-3">
          <ErrorBanner message={capitalError} />
        </div>
        <form onSubmit={saveCapital} className="flex flex-wrap items-end gap-3">
          <FormField label="Amount" className="w-44">
            <input type="number" min="0.01" step="0.01" value={capitalInput} onChange={(e) => setCapitalInput(e.target.value)} className={inputClass} />
          </FormField>
          <button type="submit" disabled={savingCapital} className={primaryButton}>
            {savingCapital ? "Saving..." : "Save"}
          </button>
        </form>
        <SavedNote show={capitalSaved} children="Trading capital saved." />
      </Card>

      <form onSubmit={save} className="rounded-card border border-line bg-surface p-4">
        <CardTitle right={<HelpTip text="You can change your pair, market, risk, or pair selection mode at any time. Switching any of them is blocked while a trade is open, so nothing gets left unmanaged." />}>
          Trading settings
        </CardTitle>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 shell:grid-cols-4">
          <FormField label="Pair selection">
            <select value={pairSelectionMode} onChange={(e) => setPairSelectionMode(e.target.value)} className={inputClass}>
              <option value="manual">Manual</option>
              <option value="auto">Auto Select</option>
            </select>
          </FormField>

          <FormField label={`Pair${pairSelectionMode === "auto" ? " (Auto Select active)" : ""}`}>
            <select value={pair} onChange={(e) => setPair(e.target.value)} disabled={pairSelectionMode === "auto"} className={inputClass}>
              {pairList.map((p) => (
                <option key={p.pair} value={p.pair} disabled={!p.available}>
                  {optionLabel(p)}
                </option>
              ))}
              {UPCOMING_PAIRS.map((p) => (
                <option key={p} value={p} disabled>
                  {p} - coming soon
                </option>
              ))}
            </select>
          </FormField>

          <FormField label="Market">
            <select value={marketType} onChange={(e) => setMarketType(e.target.value)} className={inputClass}>
              {MARKET_TYPES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </FormField>

          <FormField label="Risk per trade (%)" hint="Between 0.1 and 2.">
            <input type="number" min="0.1" max="2" step="0.1" value={riskPct} onChange={(e) => setRiskPct(e.target.value)} className={inputClass} />
          </FormField>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button type="submit" disabled={!dirty || saving} className={primaryButton}>
            {saving ? "Saving..." : "Save changes"}
          </button>
          {dirty && !saving && <span className="text-sm text-muted">You have unsaved changes.</span>}
        </div>
        <SavedNote show={saved && !dirty} children="Settings saved." />
      </form>
    </div>
  );
}
