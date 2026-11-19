import { useEffect, useState } from "react";

import RiskPctField, { acksComplete, riskFieldProblem } from "../components/RiskPctField.jsx";

import { api } from "../api/client.js";
import BinanceSettingsLine from "../components/BinanceSettingsLine.jsx";
import DailyLossLimitField from "../components/DailyLossLimitField.jsx";
import HelpTip from "../components/HelpTip.jsx";
import PairSkipNotice from "../components/PairSkipNotice.jsx";
import AdviceNotes, { useAdvice } from "../components/AdviceNotes.jsx";
import { buildRequest } from "../lib/advice.js";
import InfoHelp from "../components/InfoHelp.jsx";
import SettingsTabs, { tabPanelProps } from "../components/SettingsTabs.jsx";
import { LEVERAGE_HELP } from "../leverageHelp.js";
import { FormField, SavedNote, inputClass, primaryButton } from "../components/form.jsx";
import { Card, CardTitle, ErrorBanner, PageHeader, Pill } from "../components/ui.jsx";
import { MARKET_TYPES, UPCOMING_PAIRS } from "../constants.js";
import { usd } from "../lib/format.js";
import { useAccount } from "../context/AccountContext.jsx";
import { TABS, limitProblem, tabForMessage } from "../lib/lossLimit.js";
import { riskBand } from "../lib/riskBands.js";
import { capText, listToSave, maxExtraPairs, sameList, toggleExtra } from "../lib/livePairs.js";
import { optionLabel, usePairs, withCurrentPair } from "../usePairs.js";

// Bot Settings: two tabs on one page, one Save for both. Trading: pair selection, pair, market and trading capital. Risk: risk per trade (with its acknowledgments), leverage and the
// daily loss limit. A problem is shown on the tab it belongs to, and a tab with unsaved changes or a problem says so.
export default function BotSettings() {
  const { account, reloadAccount } = useAccount();
  const [tab, setTab] = useState("trading");
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
  const [liveSlots, setLiveSlots] = useState(String(account.live_slots || 1));
  const [extraPairs, setExtraPairs] = useState(account.live_pair_list || []);
  const [capitalInput, setCapitalInput] = useState(account.allocated_capital != null ? String(account.allocated_capital) : "");
  // The saved baseline is the amount the customer last chose: a raise that waits for midnight counts as saved, so the form is not "unsaved" after a raise.
  const savedLimit = account.daily_loss_limit_pending_usd != null ? String(account.daily_loss_limit_pending_usd) : account.daily_loss_limit_usd != null ? String(account.daily_loss_limit_usd) : "";
  const [limitInput, setLimitInput] = useState(savedLimit);
  const [problems, setProblems] = useState({ trading: null, risk: null }); // tab id -> the message shown inside that tab
  const [error, setError] = useState(null); // the reset-form error only; save problems live in `problems`
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

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

  const savedCapital = account.allocated_capital != null ? String(account.allocated_capital) : "";
  const slotsDirty = account.live_multi_available && liveSlots !== String(account.live_slots || 1);
  const extraToSave = account.live_multi_available ? listToSave(extraPairs, { slots: Number(liveSlots), primary: pair }) : [];
  const extraDirty = account.live_multi_available && !sameList(extraToSave, account.live_pair_list || []);
  const tradingDirty = extraDirty || pair !== account.pair || marketType !== account.market_type || pairSelectionMode !== account.pair_selection_mode || capitalInput !== savedCapital || slotsDirty;
  const riskDirty = riskPct !== String(account.risk_pct) || (marketType === "futures" && maxLeverage !== String(account.max_leverage || 3)) || limitInput !== savedLimit;
  const dirty = tradingDirty || riskDirty;

  // Problems the screen can see before it sends anything, per tab.
  function findProblems() {
    const found = { trading: null, risk: null };
    const cap = Number(capitalInput);
    if (capitalInput !== savedCapital && (capitalInput === "" || !(cap > 0))) found.trading = "Enter a trading capital amount greater than 0.";
    const riskProblem = riskFieldProblem(riskPct, riskLimit);
    if (riskProblem) found.risk = riskProblem;
    else if (!acksComplete(riskPct, acks)) found.risk = "Tick the acknowledgment for this risk setting before saving.";
    else {
      const lp = limitInput !== savedLimit ? limitProblem(limitInput, account.daily_loss_limit_max_usd) : null;
      if (lp) found.risk = lp;
    }
    return found;
  }
  const limitFieldProblem = limitInput !== savedLimit ? limitProblem(limitInput, account.daily_loss_limit_max_usd) : null;
  // A message shown on a tab goes away as soon as the customer edits anything again.
  const editKey = [pair, marketType, riskPct, pairSelectionMode, maxLeverage, capitalInput, limitInput, liveSlots, extraPairs.join(","), JSON.stringify(acks)].join("|");
  useEffect(() => {
    setProblems({ trading: null, risk: null });
  }, [editKey]);
  const advice = useAdvice(buildRequest({ account, pair, marketType, riskPct, pairSelectionMode, maxLeverage, capitalInput, limitInput, liveSlots, acks, savedCapital, savedLimit }));
  const marks = {
    trading: problems.trading ? "error" : tradingDirty ? "changed" : null,
    risk: problems.risk ? "error" : riskDirty ? "changed" : null,
  };

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
    setSaved(false);
    const found = findProblems();
    setProblems(found);
    if (found.trading || found.risk) {
      setTab(found.trading && (tab === "trading" || !found.risk) ? "trading" : "risk");
      return;
    }
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
        allocated_capital: capitalInput !== savedCapital ? Number(capitalInput) : undefined,
        live_slots: slotsDirty ? Number(liveSlots) : undefined,
        pair_list: extraDirty ? extraToSave : undefined,
        daily_loss_limit_usd: limitInput !== savedLimit ? Number(limitInput) : undefined,
      });
      await reloadAccount();
      setSaved(true);
    } catch (err) {
      const where = tabForMessage(err.message);
      setProblems({ trading: null, risk: null, [where]: err.message });
      setTab(where);
    } finally {
      setSaving(false);
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

      {account.total_loss_stop_active && (
        <Card>
          <CardTitle>Automatic total loss stop</CardTitle>
          <p role="alert" className="rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm font-medium text-fg">
            Stopped: your account is {account.total_loss_stop_threshold_pct}% or more below its reference value
            {account.total_loss_reference_equity != null ? ` (${usd(account.total_loss_reference_equity)})` : ""}, so new trades are off. Trades that are already open keep their stops. New trades stay
            off until you reset this.
          </p>
          <form onSubmit={resetTotalLoss} className="mt-3 grid gap-2">
            <label className="flex items-start gap-2 text-sm text-fg">
              <input type="checkbox" className="mt-1 h-4 w-4" checked={resetConfirm} onChange={(e) => setResetConfirm(e.target.checked)} />
              <span>I confirm. The reference value is taken again from my balance at the next check, and new trades can start again. Use this after you deposit or withdraw money too.</span>
            </label>
            <div>
              <button type="submit" disabled={!resetConfirm || resetting} className={primaryButton}>
                {resetting ? "Resetting..." : "Reset and allow new trades"}
              </button>
            </div>
          </form>
        </Card>
      )}

      <form onSubmit={save} className="rounded-card border border-line bg-surface p-4" noValidate>
        <SettingsTabs tabs={TABS} active={tab} onChange={setTab} marks={marks} label="Bot settings sections" />

        <div {...tabPanelProps("trading", tab)} className="pt-4">
          <CardTitle right={<HelpTip text="You can change your risk per trade, leverage and trading capital at any time; they apply to new trades only. Your pair, market and pair selection mode cannot be changed while a trade is open, so nothing gets left unmanaged." />}>
            Trading
          </CardTitle>
          <div className="mb-3"><PairSkipNotice account={account} /></div>
          {problems.trading && <p role="alert" className="mb-3 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm font-medium text-fg">{problems.trading}</p>}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 shell:grid-cols-3">
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
          </div>

          <div className="mt-4 max-w-xs">
            <div className="flex items-center gap-1">
              <label htmlFor="trading-capital" className="text-sm font-medium text-fg">
                Trading capital (US dollars)
              </label>
              <HelpTip text="The maximum amount of your Binance balance the bot may use as its sizing base. Your funds stay in your own Binance account at all times - this is a ceiling, not a transfer. The bot will never use more than what Binance actually shows as available, even if this is set higher." />
            </div>
            <div className="relative">
              <span aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted">$</span>
              <input id="trading-capital" type="number" min="0.01" step="0.01" value={capitalInput} onChange={(e) => setCapitalInput(e.target.value)} className={`${inputClass} pl-7`} aria-describedby="trading-capital-hint" />
            </div>
            <span id="trading-capital-hint" className="mt-1 block text-xs text-muted">
              {account.allocated_capital != null
                ? `Currently ${usd(account.allocated_capital)}. You can raise or lower it at any time, even with a trade open - it only affects your bot's next new trade.`
                : "Not configured yet. Required before you can start the bot on a live account."}
            </span>
          </div>

          {account.live_multi_available && (
            <div className="mt-4 max-w-xs">
              <label htmlFor="live-slots" className="text-sm font-medium text-fg">
                Open trades at once
              </label>
              <select id="live-slots" value={liveSlots} onChange={(e) => setLiveSlots(e.target.value)} className={inputClass} aria-describedby="live-slots-hint">
                {[1, 2, 3].map((n) => (
                  <option key={n} value={String(n)}>
                    {n}
                  </option>
                ))}
              </select>
              <span id="live-slots-hint" className="mt-1 block text-xs text-muted">
                The most trades the bot may hold at the same time, one per pair, 1 to 3. More than one needs Manual pair selection, and the bot only trades the pairs set for your account (you have {account.live_pair_count || 1}). The daily loss limit and the automatic total stop cover all your trades together. Lowering it never closes a trade.
              </span>
            </div>
          )}
          {account.live_multi_available && Number(liveSlots) > 1 && (
            <fieldset className="mt-4 max-w-md">
              <legend className="text-sm font-medium text-fg">Extra pairs to trade</legend>
              <span className="mt-1 block text-xs text-muted">
                The bot trades your main pair first, then these, one trade per pair. Choose up to {maxExtraPairs(liveSlots)}. Needs Manual pair selection. A pair with an open trade cannot be removed until that trade closes.
              </span>
              <div className="mt-2 grid gap-1">
                {pairList.filter((p) => p.pair !== pair).map((p) => {
                  const checked = extraPairs.includes(p.pair);
                  const full = !checked && listToSave(extraPairs, { slots: Number(liveSlots), primary: pair }).length >= maxExtraPairs(liveSlots);
                  return (
                    <label key={p.pair} className="flex min-h-[44px] items-center gap-2 text-sm text-fg">
                      <input type="checkbox" className="h-4 w-4" checked={checked} disabled={(!p.available && !checked) || full} onChange={() => setExtraPairs((l) => toggleExtra(l, p.pair, { slots: Number(liveSlots), primary: pair }))} />
                      <span>{optionLabel(p)}</span>
                    </label>
                  );
                })}
              </div>
              {capText({ capPct: account.live_open_risk_cap_pct, riskPct: Number(riskPct), slots: Number(liveSlots), capital: capitalInput !== "" ? Number(capitalInput) : account.allocated_capital }) && (
                <p className="mt-2 text-xs text-muted">{capText({ capPct: account.live_open_risk_cap_pct, riskPct: Number(riskPct), slots: Number(liveSlots), capital: capitalInput !== "" ? Number(capitalInput) : account.allocated_capital })}</p>
              )}
            </fieldset>
          )}
          <AdviceNotes advice={advice} tab="trading" />
        </div>

        <div {...tabPanelProps("risk", tab)} className="pt-4">
          <CardTitle right={<HelpTip text="Risk per trade, leverage and your daily loss limit apply to new trades only. An open trade keeps its stop-loss." />}>
            Risk
          </CardTitle>
          {problems.risk && <p role="alert" className="mb-3 rounded-ctl border border-line border-l-4 border-l-neg bg-neg-soft px-3 py-2 text-sm font-medium text-fg">{problems.risk}</p>}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
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

          <div className="mt-4 max-w-md">
            <DailyLossLimitField
              value={limitInput}
              onChange={setLimitInput}
              account={account}
              riskPct={riskPct}
              capital={capitalInput !== "" ? Number(capitalInput) : account.allocated_capital}
              problem={limitFieldProblem}
            />
          </div>
          <AdviceNotes advice={advice} tab="risk" />
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-4">
          <button type="submit" disabled={!dirty || saving} className={primaryButton}>
            {saving ? "Saving..." : "Save changes"}
          </button>
          {dirty && !saving && (
            <span role="status" className="text-sm text-muted">
              You have unsaved changes{tradingDirty && riskDirty ? " on both tabs" : tradingDirty ? " on the Trading tab" : " on the Risk tab"}.
            </span>
          )}
        </div>
        <SavedNote show={saved && !dirty} children="Settings saved." />
      </form>
    </div>
  );
}
