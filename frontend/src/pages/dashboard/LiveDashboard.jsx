import { useEffect, useState } from "react";

import { api } from "../../api/client.js";
import BotStatusCard from "../../components/BotStatusCard.jsx";
import { Badge, Card, EmptyState, PnlText, StatTile } from "../../components/ui.jsx";
import { navigate } from "../../useHashRoute.js";

export default function LiveDashboard({ account }) {
  const [balance, setBalance] = useState(undefined); // undefined = loading, null = unavailable
  const [summary, setSummary] = useState(null);
  const [position, setPosition] = useState(undefined);
  const [recent, setRecent] = useState([]);

  useEffect(() => {
    api.get("/account/balance").then(setBalance).catch(() => setBalance(null));
    api.get("/trades/summary?is_paper=false").then(setSummary).catch(() => {});
    api.get("/account/position").then(setPosition).catch(() => setPosition(null));
    api
      .get("/trades?is_paper=false")
      .then((trades) => setRecent(trades.filter((t) => t.status === "closed").slice(0, 5)))
      .catch(() => {});
  }, [account.id]);

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Badge tone="red">LIVE</Badge>
        <Badge tone="slate">
          {account.pair} · {account.market_type}
        </Badge>
      </div>

      <BotStatusCard account={account} />

      <h2 className="mt-6 mb-2 text-sm font-semibold text-slate-700">Account Overview</h2>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatTile
          label="Exchange Balance"
          value={balance === undefined ? "..." : balance === null ? "Unavailable" : `$${balance.available_balance.toFixed(2)}`}
          help="The free balance in the quote currency of your trading pair, read directly from your Binance account."
        />
        <StatTile label="Today" value={<PnlText value={summary?.pnl_today ?? null} />} />
        <StatTile label="7D" value={<PnlText value={summary?.pnl_7d ?? null} />} />
        <StatTile label="30D" value={<PnlText value={summary?.pnl_30d ?? null} />} />
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatTile label="All-Time P&L" value={<PnlText value={summary?.net_pnl ?? null} />} />
        <StatTile label="Total Trades" value={summary?.total_trades ?? "-"} />
        <StatTile
          label="Win Rate"
          value={summary?.win_rate === null || summary?.win_rate === undefined ? "-" : `${(summary.win_rate * 100).toFixed(0)}%`}
        />
      </div>

      <h2 className="mt-6 mb-2 text-sm font-semibold text-slate-700">Current Position</h2>
      {position === undefined ? (
        <Card>
          <p className="text-sm text-slate-400">Loading...</p>
        </Card>
      ) : !position.has_position ? (
        <EmptyState
          title="No open position"
          body="The bot is monitoring the market for qualifying opportunities."
        />
      ) : (
        <Card>
          <div className="flex items-center justify-between">
            <p className="text-sm font-semibold text-ink">
              {position.side.toUpperCase()} {position.pair}
            </p>
            <PnlText value={position.unrealized_pnl} className="font-semibold" />
          </div>
          <dl className="mt-3 grid grid-cols-2 gap-y-2 text-sm text-slate-600 sm:grid-cols-4">
            <Field label="Entry" value={position.entry_price} />
            <Field label="Current" value={position.current_price ?? "-"} />
            <Field label="Size" value={position.quantity} />
            <Field label="Opened" value={new Date(position.opened_at).toLocaleString()} />
          </dl>
        </Card>
      )}

      <div className="mt-6 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-700">Recent Trades</h2>
        <button onClick={() => navigate("trade-history")} className="text-sm text-accent hover:underline">
          View All
        </button>
      </div>
      {recent.length === 0 ? (
        <EmptyState title="No trades yet" body="The bot is monitoring the market for qualifying opportunities." />
      ) : (
        <div className="space-y-2">
          {recent.map((t) => (
            <Card key={t.id} className="flex items-center justify-between py-3">
              <div>
                <p className="text-sm font-medium text-ink">
                  {t.side.toUpperCase()} {account.pair}
                </p>
                <p className="text-xs text-slate-500">{new Date(t.opened_at).toLocaleDateString()}</p>
              </div>
              <PnlText value={t.pnl} className="font-semibold" />
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function Field({ label, value }) {
  return (
    <div>
      <dt className="text-xs uppercase text-slate-400">{label}</dt>
      <dd className="font-medium text-ink">{value}</dd>
    </div>
  );
}
