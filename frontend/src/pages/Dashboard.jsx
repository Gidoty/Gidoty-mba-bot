import { useEffect, useState } from "react";

import { api } from "../api/client.js";

export default function Dashboard({ account, onLogout, onAccountChanged }) {
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

  async function goToBilling() {
    setError(null);
    try {
      const { portal_url } = await api.get("/billing/portal").catch(async () => {
        const { checkout_url } = await api.post("/billing/checkout-session");
        return { portal_url: checkout_url };
      });
      window.location.href = portal_url;
    } catch (err) {
      setError(err.message);
    }
  }

  const trialEndsAt = new Date(account.trial_ends_at);
  const daysLeft = Math.max(0, Math.ceil((trialEndsAt - new Date()) / (1000 * 60 * 60 * 24)));

  return (
    <div className="min-h-screen bg-slate-50 px-4 py-10">
      <div className="mx-auto max-w-3xl">
        <div className="mb-6 flex items-center justify-between">
          <span className="text-lg font-semibold text-ink">MBA Bot</span>
          <div className="space-x-4 text-sm">
            <button onClick={goToBilling} className="text-slate-500 hover:text-ink">
              Billing
            </button>
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

        <div className="mt-6 flex flex-wrap gap-3">
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
