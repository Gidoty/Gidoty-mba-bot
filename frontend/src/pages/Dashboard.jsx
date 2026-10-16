import { useEffect, useState } from "react";

import { api } from "../api/client.js";

export default function Dashboard({ customer, account, onLogout, onAccountChanged, onCustomerChanged }) {
  const [trades, setTrades] = useState([]);
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.get("/trades").then(setTrades).catch(() => {});
    api.get("/trades/summary").then(setSummary).catch(() => {});
  }, [account]);

  async function togglePause() {
    setError(null);
    try {
      await api.patch("/account", { is_active: !account.is_active });
      onAccountChanged();
    } catch (err) {
      setError(err.message);
    }
  }

  async function acceptLiveBypass() {
    setError(null);
    try {
      await api.post("/account/live-bypass", { accept: true });
      onAccountChanged();
    } catch (err) {
      setError(err.message);
    }
  }

  async function startPayment() {
    setError(null);
    try {
      const { authorization_url } = await api.post("/billing/initialize");
      window.location.href = authorization_url;
    } catch (err) {
      setError(err.message);
    }
  }

  async function refreshPaymentStatus() {
    setError(null);
    await onCustomerChanged();
  }

  const trialEndsAt = new Date(account.trial_ends_at);
  const daysLeft = Math.max(0, Math.ceil((trialEndsAt - new Date()) / (1000 * 60 * 60 * 24)));

  return (
    <div className="min-h-screen bg-slate-50 px-4 py-10">
      <div className="mx-auto max-w-3xl">
        <div className="mb-6 flex items-center justify-between">
          <span className="text-lg font-semibold text-ink">MBA Bot</span>
          <div className="space-x-4 text-sm">
            <button onClick={onLogout} className="text-slate-500 hover:text-ink">
              Log out
            </button>
          </div>
        </div>

        {error && <p className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <StatCard label="Mode" value={account.mode === "live" ? "Live" : `Paper (${daysLeft}d left)`} />
          <StatCard label="Pair" value={`${account.pair} · ${account.market_type}`} />
          <StatCard label="Risk per trade" value={`${account.risk_pct}%`} />
        </div>

        {summary && (
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-3">
            <StatCard label="Net P&L" value={summary.net_pnl.toFixed(2)} />
            <StatCard label="Open trades" value={summary.open_trades} />
            <StatCard
              label="Win rate"
              value={summary.win_rate === null ? "-" : `${(summary.win_rate * 100).toFixed(0)}%`}
            />
          </div>
        )}

        <TelegramStatus account={account} onAccountChanged={onAccountChanged} />

        {customer.has_paid ? (
          <div className="mt-6 rounded-lg border border-accent/30 bg-accent/5 p-4">
            <p className="text-sm font-medium text-accent-dark">Unlocked - lifetime access active.</p>
          </div>
        ) : (
          <div className="mt-6 rounded-lg border border-red-200 bg-red-50 p-4">
            <p className="text-sm font-semibold text-red-800">Locked - the bot is not trading on this account yet.</p>
            <p className="mt-1 text-sm text-red-700">
              Every account starts locked. Pay to unlock it yourself, or ask whoever gave you this link to unlock it
              for you.
            </p>
            <div className="mt-3 flex flex-wrap gap-3">
              <button
                onClick={startPayment}
                className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-dark"
              >
                Pay to unlock
              </button>
              <button
                onClick={refreshPaymentStatus}
                className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-ink hover:bg-slate-50"
              >
                I've paid - refresh status
              </button>
            </div>
          </div>
        )}

        {customer.has_paid && (
          <div className="mt-4 flex flex-wrap gap-3">
            <button
              onClick={togglePause}
              className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-ink hover:bg-slate-50"
            >
              {account.is_active ? "Pause trading" : "Resume trading"}
            </button>
            {account.mode === "paper" && (
              <button
                onClick={acceptLiveBypass}
                className="rounded-md border border-amber-300 bg-amber-50 px-4 py-2 text-sm font-medium text-amber-800 hover:bg-amber-100"
              >
                Skip trial and trade live
              </button>
            )}
          </div>
        )}

        <h2 className="mt-10 mb-3 text-sm font-semibold text-slate-700">Trade history</h2>
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-50 text-xs uppercase text-slate-500">
              <tr>
                <Th>Opened</Th>
                <Th>Side</Th>
                <Th>Entry</Th>
                <Th>Exit</Th>
                <Th>P&L</Th>
                <Th>Status</Th>
              </tr>
            </thead>
            <tbody>
              {trades.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-slate-400">
                    No trades yet.
                  </td>
                </tr>
              )}
              {trades.map((t) => (
                <tr key={t.id} className="border-t border-slate-100">
                  <td className="px-4 py-2">{new Date(t.opened_at).toLocaleString()}</td>
                  <td className="px-4 py-2 uppercase">{t.side}</td>
                  <td className="px-4 py-2">{t.entry_price}</td>
                  <td className="px-4 py-2">{t.exit_price ?? "-"}</td>
                  <td className={`px-4 py-2 ${t.pnl > 0 ? "text-accent-dark" : t.pnl < 0 ? "text-red-600" : ""}`}>
                    {t.pnl === null ? "-" : t.pnl.toFixed(2)}
                  </td>
                  <td className="px-4 py-2">{t.is_paper ? "paper" : "live"} · {t.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function TelegramStatus({ account, onAccountChanged }) {
  const [linkUrl, setLinkUrl] = useState(null);
  const [error, setError] = useState(null);
  const [checking, setChecking] = useState(false);

  const linked = Boolean(account.notification_target);

  async function getLink() {
    setError(null);
    try {
      const { link_url } = await api.get("/telegram/link-url");
      setLinkUrl(link_url);
      window.open(link_url, "_blank", "noopener");
    } catch (err) {
      setError(err.message);
    }
  }

  async function checkLinked() {
    setChecking(true);
    try {
      await onAccountChanged();
    } finally {
      setChecking(false);
      setLinkUrl(null);
    }
  }

  return (
    <div className="mt-4 rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-ink">Telegram</span>
        <span
          className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
            linked ? "bg-accent/10 text-accent-dark" : "bg-red-50 text-red-700"
          }`}
        >
          {linked ? "Linked" : "Not linked"}
        </span>
      </div>

      {!linked && (
        <p className="mt-1 text-xs text-slate-500">
          The bot won't send (or trade past the trial without) alerts until this is linked.
        </p>
      )}

      {error && <p className="mt-2 rounded-md bg-red-50 px-3 py-2 text-xs text-red-700">{error}</p>}

      <div className="mt-3 flex flex-wrap gap-3">
        <button
          onClick={getLink}
          className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-ink hover:bg-slate-50"
        >
          {linked ? "Relink Telegram" : "Open Telegram and link"}
        </button>
        {linkUrl && (
          <button
            onClick={checkLinked}
            disabled={checking}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-dark disabled:opacity-60"
          >
            {checking ? "Checking..." : "I've linked it - refresh"}
          </button>
        )}
      </div>
    </div>
  );
}

function StatCard({ label, value }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <dt className="text-xs uppercase text-slate-500">{label}</dt>
      <dd className="mt-1 text-lg font-semibold text-ink">{value}</dd>
    </div>
  );
}

function Th({ children }) {
  return <th className="px-4 py-2 font-medium">{children}</th>;
}
