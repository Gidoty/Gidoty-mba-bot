import { useEffect, useState } from "react";

import { api } from "../api/client.js";
import HelpTip from "../components/HelpTip.jsx";

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
          <div className="flex items-center gap-2">
            <span className="text-lg font-semibold text-ink">MBA Bot</span>
            {account.bot_code && (
              <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 font-mono text-xs text-slate-600">
                {account.bot_code}
                <HelpTip text="Your bot's reference code. Keep it handy - if you ever contact support, this is the fastest way for us to find your account." />
              </span>
            )}
          </div>
          <div className="space-x-4 text-sm">
            <button onClick={onLogout} className="text-slate-500 hover:text-ink">
              Log out
            </button>
          </div>
        </div>

        {error && <p className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <StatCard
            label="Mode"
            value={account.mode === "live" ? "Live" : customer.has_paid ? `Paper (${daysLeft}d left)` : "Paper (free trial)"}
            help={
              account.mode === "live"
                ? "Trading with your real Binance funds."
                : "Simulated trades against Binance testnet - no real money is ever at risk in this mode."
            }
          />
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
            <p className="text-sm font-medium text-accent-dark">Unlocked - lifetime access to live trading.</p>
          </div>
        ) : (
          <div className="mt-6 rounded-lg border border-amber-200 bg-amber-50 p-4">
            <p className="text-sm font-semibold text-amber-800">
              Free trial - paper trading only
              <HelpTip text="Unpaid accounts trade on Binance testnet forever, for free - there's no time limit and no risk to real funds. Paying is only required to switch this account to live trading with your own money." />
            </p>
            <p className="mt-1 text-sm text-amber-700">
              You can keep testing the bot on paper for as long as you like, at no cost. When you're ready to trade
              with real funds, pay to unlock live trading.
            </p>
            <div className="mt-3 flex flex-wrap gap-3">
              <button
                onClick={startPayment}
                className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-dark"
              >
                Pay to unlock live trading
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

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-1">
            <button
              onClick={togglePause}
              className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-ink hover:bg-slate-50"
            >
              {account.is_active ? "Pause trading" : "Resume trading"}
            </button>
            <HelpTip text="Pausing stops the bot from opening new trades on this account - a trade already open is still managed through to its exit. Resume anytime." />
          </div>
          {account.mode === "paper" && !account.live_bypass_accepted_at && (
            <div className="flex items-center gap-1">
              <button
                onClick={acceptLiveBypass}
                className="rounded-md border border-amber-300 bg-amber-50 px-4 py-2 text-sm font-medium text-amber-800 hover:bg-amber-100"
              >
                {customer.has_paid ? "Skip trial and trade live" : "Accept live-trading disclosure"}
              </button>
              <HelpTip
                text={
                  customer.has_paid
                    ? "Switches this account to live trading right now, with your own funds, instead of waiting out the rest of the 7-day trial."
                    : "Records your agreement to trade live now. It has no effect until you pay - the moment you unlock live trading, this account switches to live immediately with no extra step."
                }
              />
            </div>
          )}
          {account.mode === "paper" && account.live_bypass_accepted_at && !customer.has_paid && (
            <p className="text-xs text-slate-500">
              Live-trading disclosure accepted - this account will switch to live the moment you unlock it.
            </p>
          )}
        </div>

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
        <span className="text-sm font-medium text-ink">
          Telegram
          <HelpTip text="The bot messages this chat whenever it opens or closes a trade, plus a daily P&L summary. It won't trade at all, paper or live, until this is linked." />
        </span>
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

function StatCard({ label, value, help }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <dt className="text-xs uppercase text-slate-500">
        {label}
        {help && <HelpTip text={help} />}
      </dt>
      <dd className="mt-1 text-lg font-semibold text-ink">{value}</dd>
    </div>
  );
}

function Th({ children }) {
  return <th className="px-4 py-2 font-medium">{children}</th>;
}
