import { useEffect, useState } from "react";

import RiskPctField, { acksComplete, riskFieldProblem } from "../components/RiskPctField.jsx";

import { api } from "../api/client.js";
import HelpTip from "../components/HelpTip.jsx";
import BinanceSettingsLine from "../components/BinanceSettingsLine.jsx";
import InfoHelp from "../components/InfoHelp.jsx";
import { LEVERAGE_HELP } from "../leverageHelp.js";
import { FormField, SavedNote, inputClass, primaryButton } from "../components/form.jsx";
import { Card, CardTitle, ErrorBanner, PageHeader, Pill, ThemeSwitch } from "../components/ui.jsx";
import { MARKET_TYPES, UPCOMING_PAIRS } from "../constants.js";
import { usd } from "../lib/format.js";
import { useAccount } from "../context/AccountContext.jsx";
import { riskBand } from "../lib/riskBands.js";
import { optionLabel, usePairs, withCurrentPair } from "../usePairs.js";

export default function BotSettings() {
  const { account, reloadAccount } = useAccount();
  const [pair, setPair] = useState(account.pair);
  const [marketType, setMarketType] = useState(account.market_type);
  const [riskPct, setRiskPct] = useState(String(account.risk_pct));
  const [pairSelectionMode, setPairSelectionMode] = useState(account.pair_selection_mode);
  const riskLimit = account.risk_pct_limit || 2;
  const [acks, setAcks] = useState({});
  const [resetting, setResetting] = useState(false);
  const [resetConfirm, setResetConfirm] = useState(false);
  const leverageLimit = account.max_leverage_limit || 5;
  const [maxLeverage, setMaxLeverage] = useState(String(account.max_leverage || 3));
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
    pairSelectionMode !== account.pair_selection_mode ||
    (marketType === "futures" && maxLeverage !== String(account.max_leverage || 3));
  const riskInvalid = riskFieldProblem(riskPct, riskLimit) !== null;
  const needsAcks = !acksComplete(riskPct, acks);

  async function resetTotalLoss(e) {
    e.preventDefault();
    setError(null);
    setResetting(true);
    try {
      await api.post("/account/total-loss-reset", { confirm: true });
      await reloadAccount();
      setResetConfirm(false);
    } catch (err) {
      setError(err.message);
    } finally {
      setResetting(false);
    }
  }

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
        acknowledge_high_risk: !!acks.high,
        acknowledge_very_high_risk: !!acks.very_high,
        pair_selection_mode: pairSelectionMode,
        // Futures only; applies to new trades only, an open trade is not touched.
        max_leverage: marketType === "futures" ? Number(maxLeverage) : undefined,
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
          <Pill>{account.risk_pct}% risk per trade{riskBand(account.risk_pct) === "high" ? " - higher risk" : riskBand(account.risk_pct) === "very_high" ? " - very high risk" : ""}</Pill>
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
            ? `Currently set to ${usd(account.allocated_capital)}. You can raise or lower it at any time, even with a trade open - it only affects your bot's next new trade.`
            : "Not configured yet. Required before you can start the bot on a live account."}
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
            {savingCapital ? "Saving..." : "Save"}
          </button>
        </form>
        <SavedNote show={capitalSaved} children="Trading capital saved." />
      </Card>

      <form onSubmit={save} className="rounded-card border border-line bg-surface p-4">
        <CardTitle right={<HelpTip text="You can change your risk per trade, leverage and trading capital at any time; they apply to new trades only. Your pair, market and pair selection mode cannot be changed while a trade is open, so nothing gets left unmanaged." />}>
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

          <RiskPctField value={riskPct} onChange={setRiskPct} limit={riskLimit} acks={acks} onAcksChange={setAcks} />

          {marketType === "futures" && (
            <div>
              <div className="flex items-center gap-1">
                <label htmlFor="max-leverage" className="text-sm font-medium text-fg">
                  Leverage (Futures)
                </label>
                <InfoHelp label="the leverage setting" help={LEVERAGE_HELP} />
              </div>
              <select id="max-leverage" value={maxLeverage} onChange={(e) => setMaxLeverage(e.target.value)} className={inputClass} aria-describedby="max-leverage-hint">
                {Array.from({ length: leverageLimit }, (_, i) => i + 1).map((n) => (
                  <option key={n} value={String(n)}>
                    {n}x
                  </option>
                ))}
              </select>
              <span id="max-leverage-hint" className="mt-1 block text-xs text-muted">
                {`Whole number from 1 to ${leverageLimit}. The most a position can be worth compared with your trading capital. Applies to new trades only.`}
              </span>
              <BinanceSettingsLine autoSelect={account.pair_selection_mode === "auto"} />
            </div>
          )}
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button type="submit" disabled={!dirty || saving || riskInvalid || needsAcks} className={primaryButton}>
            {saving ? "Saving..." : "Save changes"}
          </button>
          {dirty && !saving && <span className="text-sm text-muted">You have unsaved changes.</span>}
        </div>
        <SavedNote show={saved && !dirty} children="Settings saved." />
      </form>

      {account.total_loss_stop_enabled && (
        <Card>
          <CardTitle>Total loss limit</CardTitle>
          <p className="text-sm text-fg">
            New trades stop if your account falls {account.total_loss_stop_threshold_pct}% below its reference value
            {account.total_loss_reference_equity != null ? ` (${usd(account.total_loss_reference_equity)})` : " (set at the next check)"}. Trades that are already open keep their
            stops. This limit follows your risk setting: it is 10% or three times your risk per trade, whichever is higher, up to 80%.
          </p>
          {account.total_loss_stop_active && (
            <p role="alert" className="mt-3 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm font-medium text-fg">
              Stopped: new trades are off because the limit was reached. They stay off until you reset this.
            </p>
          )}
          <form onSubmit={resetTotalLoss} className="mt-3 grid gap-2">
            <label className="flex items-start gap-2 text-sm text-fg">
              <input type="checkbox" className="mt-1 h-4 w-4" checked={resetConfirm} onChange={(e) => setResetConfirm(e.target.checked)} />
              <span>
                I confirm. The reference value is taken again from my balance at the next check{account.total_loss_stop_active ? ", and new trades can start again" : ""}. Use this after you
                deposit or withdraw money so the limit measures from the right value.
              </span>
            </label>
            <div>
              <button type="submit" disabled={!resetConfirm || resetting} className={primaryButton}>
                {resetting ? "Resetting..." : account.total_loss_stop_active ? "Reset and allow new trades" : "Reset the reference value"}
              </button>
            </div>
          </form>
        </Card>
      )}
    </div>
  );
}
