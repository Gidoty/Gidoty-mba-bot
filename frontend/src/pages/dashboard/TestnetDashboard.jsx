import { useEffect, useState } from "react";

import { api } from "../../api/client.js";
import BotStatusCard from "../../components/BotStatusCard.jsx";
import HelpTip from "../../components/HelpTip.jsx";
import { Badge, Card, ErrorBanner, PnlText, StatTile } from "../../components/ui.jsx";
import { GLOSSARY } from "../../glossary.js";

export default function TestnetDashboard({ account, onAccountChanged, onActivate }) {
  const [summary, setSummary] = useState(null);
  const [trades, setTrades] = useState([]);
  const [error, setError] = useState(null);
  const [bypassing, setBypassing] = useState(false);

  useEffect(() => {
    api.get("/trades/summary?is_paper=true").then(setSummary).catch(() => {});
    api.get("/trades?is_paper=true").then(setTrades).catch(() => {});
  }, [account.id]);

  async function acceptBypass() {
    setError(null);
    setBypassing(true);
    try {
      await api.post("/account/live-bypass", { accept: true });
      await onAccountChanged();
    } catch (err) {
      setError(err.message);
    } finally {
      setBypassing(false);
    }
  }

  const daysLeft = Math.max(0, Math.ceil((new Date(account.trial_ends_at) - new Date()) / (1000 * 60 * 60 * 24)));
  const openPositions = trades.filter((t) => t.status === "open");
  const recent = trades.filter((t) => t.status === "closed").slice(0, 5);

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Badge tone="sky">TESTNET - Practice Environment</Badge>
        <Badge tone="slate">
          {account.pair} · {account.market_type}
        </Badge>
      </div>

      <ErrorBanner message={error} />

      <BotStatusCard account={account} />

      <Card className="mt-4">
        <p className="text-sm font-medium text-ink">
          <span className="mr-1">{GLOSSARY.testnet.term}</span>
          <HelpTip text={GLOSSARY.testnet.long} />
        </p>
        <p className="mt-1 text-sm text-slate-600">
          Everything on this page runs against Binance's practice environment. It behaves like the real market, but
          no real money is involved, and nothing here can be withdrawn.
        </p>
      </Card>

      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatTile
          label="Practice Balance"
          value={`$${account.paper_balance.toFixed(2)}`}
          help="A simulated balance used only for practice trades. It is not real money, was never deposited by you, and cannot be withdrawn."
        />
        <StatTile label="Test P&L" value={<PnlText value={summary?.net_pnl ?? null} />} />
        <StatTile label="Open Test Positions" value={openPositions.length} />
      </div>

      <h2 className="mt-6 mb-2 text-sm font-semibold text-slate-700">Recent Test Trades</h2>
      {recent.length === 0 ? (
        <Card>
          <p className="text-sm text-slate-500">
            No test trades yet. The bot is monitoring the market for qualifying opportunities.
          </p>
        </Card>
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

      <UnderstandingTestnet />

      <div className="mt-6 rounded-lg border border-accent/30 bg-accent/5 p-5">
        <p className="text-sm font-semibold text-ink">Ready to move beyond practice trading?</p>
        <p className="mt-1 text-sm text-slate-600">
          Activating your account unlocks the full trading environment. You'll still choose, separately and
          explicitly, if and when to connect real funds - activating does not do that by itself.
        </p>
        <button
          onClick={onActivate}
          className="mt-4 w-full rounded-md bg-accent px-4 py-3 text-sm font-semibold text-white hover:bg-accent-dark sm:w-auto"
        >
          Activate
        </button>
      </div>

      {!account.live_bypass_accepted_at && (
        <div className="mt-4 flex items-center gap-1 text-xs text-slate-500">
          <button onClick={acceptBypass} disabled={bypassing} className="underline hover:text-ink disabled:opacity-60">
            Skip the {daysLeft}-day practice period
          </button>
          <HelpTip text="Lets this account become eligible for live trading sooner, instead of waiting out the full practice period. This changes nothing by itself - you still activate, connect Binance, and enable live trading as separate, deliberate steps." />
        </div>
      )}
    </div>
  );
}

const TOPICS = ["testnet", "pair", "spot", "futures", "position", "pnl"];

function UnderstandingTestnet() {
  return (
    <details className="mt-6 rounded-lg border border-slate-200 bg-white p-4">
      <summary className="cursor-pointer text-sm font-semibold text-ink">Understanding Testnet</summary>
      <dl className="mt-3 space-y-3">
        {TOPICS.map((key) => (
          <div key={key}>
            <dt className="text-sm font-medium text-ink">{GLOSSARY[key].term}</dt>
            <dd className="text-sm text-slate-600">{GLOSSARY[key].long}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
