import { useState } from "react";

import { api } from "../api/client.js";
import { FormField, SavedNote, inputClass, primaryButton, secondaryButton } from "./form.jsx";
import { Card, CardTitle, ErrorBanner, Pill } from "./ui.jsx";

// Demo mode (DEMO_RUNNER): the customer's own DEMO keys, kept apart from the live key, and the switch for demo mode. Shown only when the service offers it. Real keys are refused by the
// server (they do not work on Binance's demo system); this panel never shows a key back.
export default function DemoPanel({ account, onAccountChanged }) {
  const [key, setKey] = useState("");
  const [secret, setSecret] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(null);
  if (!account.demo_available) return null;
  const on = account.mode === "demo";
  const market = account.market_type === "futures" ? "Binance Futures Demo Trading" : "the Binance Spot Testnet";
  const live = Boolean(account.live_trading_enabled_at || account.bot_started_at);

  async function run(fn, note) {
    setError(null);
    setSaved(null);
    setBusy(true);
    try {
      await fn();
      await onAccountChanged();
      setSaved(note);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardTitle right={<Pill tone={on ? "warn" : "mute"}>{on ? "DEMO is on" : "DEMO is off"}</Pill>}>Demo mode</CardTitle>
      <p className="text-sm text-muted">
        Runs the full bot on {market} with keys you create there. It uses no real money and is kept apart from your live and practice results. Do not use your live Binance keys here: they do not work on the demo system
        and are never accepted.
      </p>
      <div className="mt-3"><ErrorBanner message={error} /></div>
      {live ? (
        <p className="text-sm text-fg">Demo mode is not available while live trading is enabled for this account.</p>
      ) : (
        <>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              run(async () => {
                await api.put("/account/demo-credentials", { api_key: key.trim(), api_secret: secret.trim() });
                setKey("");
                setSecret("");
              }, "Demo keys saved.");
            }}
            className="grid gap-3 sm:grid-cols-2"
            autoComplete="off"
          >
            <FormField label="Demo API key">
              <input type="password" value={key} onChange={(e) => setKey(e.target.value)} className={inputClass} autoComplete="off" />
            </FormField>
            <FormField label="Demo API secret">
              <input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} className={inputClass} autoComplete="off" />
            </FormField>
            <div className="flex flex-wrap items-center gap-3 sm:col-span-2">
              <button type="submit" disabled={busy || !key.trim() || !secret.trim()} className={secondaryButton}>
                {account.demo_has_keys ? "Replace demo keys" : "Save demo keys"}
              </button>
              <span className="text-sm text-muted">{account.demo_has_keys ? "Demo keys are on file." : "No demo keys yet."}</span>
            </div>
          </form>
          <label className="mt-4 flex items-start gap-2 text-sm text-fg">
            <input type="checkbox" className="mt-1 h-4 w-4" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
            <span>I understand demo mode places practice orders on Binance&apos;s demo system and does not trade my live account.</span>
          </label>
          <div className="mt-3 flex flex-wrap gap-3">
            {!on && (
              <button type="button" disabled={busy || !confirm || !account.demo_has_keys} onClick={() => run(() => api.post("/account/demo-mode", { enabled: true, confirm: true }), "Demo mode is on.")} className={primaryButton}>
                Switch demo mode on
              </button>
            )}
            {on && (
              <button type="button" disabled={busy || !confirm} onClick={() => run(() => api.post("/account/demo-mode", { enabled: false, confirm: true }), "Demo mode is off.")} className={primaryButton}>
                Switch demo mode off
              </button>
            )}
            {account.demo_has_keys && !on && (
              <button type="button" disabled={busy} onClick={() => run(() => api.delete("/account/demo-credentials"), "Demo keys removed.")} className={secondaryButton}>
                Remove demo keys
              </button>
            )}
          </div>
        </>
      )}
      <SavedNote show={Boolean(saved)} children={saved} />
    </Card>
  );
}
