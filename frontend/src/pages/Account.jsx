import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import { emailStatusText } from "../lib/recovery.js";

import { linkButton, secondaryButton } from "../components/form.jsx";
import DataRequestCard from "../components/DataRequestCard.jsx";
import { Card, CardTitle, PageHeader, Pill } from "../components/ui.jsx";
import { MARKET_TYPES } from "../constants.js";
import { useAccount } from "../context/AccountContext.jsx";
import { dayMonthYear } from "../lib/format.js";
import { navigate } from "../useHashRoute.js";

const STATEMENT_URL = `${import.meta.env.VITE_API_URL || "/api"}/statements/trades.csv`;

function EmailPanel() {
  const [status, setStatus] = useState(null);
  const [msg, setMsg] = useState(null);
  const load = () => api.get("/auth/email/status").then(setStatus, () => setStatus({ available: false }));
  useEffect(() => { load(); }, []);
  return (
    <Card>
      <CardTitle>Email</CardTitle>
      <p className="text-sm text-fg">{emailStatusText(status)}</p>
      {status?.available && !status.verified && (
        <button type="button" className={secondaryButton} onClick={() => api.post("/auth/email/verify/request").then((r) => setMsg(r.message), (e) => setMsg(e.message))}>Send a confirmation link</button>
      )}
      {status?.available && (
        <label className="mt-3 flex items-center gap-2 text-sm text-fg">
          <input type="checkbox" checked={status.opt_out_non_essential} onChange={(e) => api.post("/auth/email/preferences", { opt_out_non_essential: e.target.checked }).then(load)} />
          Do not send me reminders and product notices by email (important account and billing messages are still sent).
        </label>
      )}
      {msg && <p className="mt-2 text-sm text-muted" role="status">{msg}</p>}
    </Card>
  );
}

export default function Account({ customer, onLogout }) {
  const { account } = useAccount();
  const [copied, setCopied] = useState(false);
  const live = account?.mode === "live";
  const market = account ? MARKET_TYPES.find((m) => m.value === account.market_type)?.label || account.market_type : null;

  async function copyCode() {
    try {
      await navigator.clipboard.writeText(account.bot_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard blocked: the code is on screen to select by hand.
    }
  }

  return (
    <div className="grid gap-4">
      <PageHeader title="Account" subtitle="Your profile." />
      <Card>
        <CardTitle>Your trade statement</CardTitle>
        <p className="text-sm text-fg">A spreadsheet file (CSV) of your closed trades with results before fees, fees, funding and net as separate columns. A column is empty when the figure was not recorded for that trade (funding is not recorded yet).</p>
        <a className={secondaryButton} href={STATEMENT_URL} download>Download statement (CSV)</a>
      </Card>
      <EmailPanel />

      <Card>
        <div className="flex items-center gap-4">
          <span aria-hidden="true" className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-brand-soft text-xl font-semibold uppercase text-brand-text">
            {customer.email.slice(0, 1)}
          </span>
          <div className="min-w-0">
            <p className="break-all text-base font-semibold text-fg">{customer.email}</p>
            <p className="mt-0.5 text-sm text-muted">Member since {dayMonthYear(new Date(customer.created_at).getTime())}</p>
          </div>
        </div>

        <dl className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Item label="Account status">
            <Pill tone={customer.has_paid ? "ok" : "warn"}>{customer.has_paid ? "Activated" : "Not activated"}</Pill>
          </Item>
          <Item label="Bot code">
            {account?.bot_code ? (
              <span className="flex flex-wrap items-center gap-3">
                <span className="font-mono text-sm">{account.bot_code}</span>
                <button type="button" onClick={copyCode} className={linkButton}>
                  {copied ? "Copied" : "Copy"}
                </button>
              </span>
            ) : (
              "-"
            )}
            <span className="mt-1 block text-xs text-muted">Have it ready if you contact support.</span>
          </Item>
        </dl>
      </Card>

      {account && (
        <Card>
          <CardTitle>Your bot at a glance</CardTitle>
          <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Item label="Mode">
              <Pill tone={live ? "bad" : "info"}>{live ? "Live" : "Practice (simulated)"}</Pill>
            </Item>
            <Item label="Pair and market">
              <span className="font-mono text-sm">{account.pair}</span> · {market}
            </Item>
            <Item label="Binance">
              <Pill tone={account.has_mainnet_credential ? "ok" : "mute"}>{account.has_mainnet_credential ? "Connected" : "Not connected"}</Pill>
            </Item>
            <Item label="Telegram">
              <Pill tone={account.notification_target ? "ok" : "warn"}>{account.notification_target ? "Linked" : "Not linked"}</Pill>
            </Item>
          </dl>
          <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2">
            <button type="button" onClick={() => navigate("bot-settings")} className={linkButton}>Bot Settings</button>
            <button type="button" onClick={() => navigate("exchange-connection")} className={linkButton}>Exchange Connection</button>
            <button type="button" onClick={() => navigate("notifications")} className={linkButton}>Notifications</button>
          </div>
        </Card>
      )}

      <Card>
        <CardTitle>Legal</CardTitle>
        <div className="flex flex-wrap gap-x-5 gap-y-2">
          <a className={linkButton} href="#/terms">Terms of Service</a>
          <a className={linkButton} href="#/privacy">Privacy Policy</a>
          <a className={linkButton} href="#/risk-disclosure">Trading Risk Disclosure</a>
        </div>
      </Card>

      <DataRequestCard />

      <button type="button" onClick={onLogout} className={`${secondaryButton} w-full`}>
        Log out
      </button>
    </div>
  );
}

function Item({ label, children }) {
  return (
    <div className="rounded-ctl border border-line bg-surface-2 px-3.5 py-3">
      <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
      <dd className="mt-1.5 text-sm text-fg">{children}</dd>
    </div>
  );
}
