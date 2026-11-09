import { useMemo, useState } from "react";

import { api } from "../../api/client.js";
import BotStatusCard from "../../components/BotStatusCard.jsx";
import { DailyCard, PnlHero, WinLossCard } from "../../components/dashboard/PerformancePanels.jsx";
import PositionCard, { NoPosition } from "../../components/dashboard/PositionCard.jsx";
import RecentTrades from "../../components/dashboard/RecentTrades.jsx";
import useTrades from "../../components/dashboard/useTrades.js";
import HelpTip from "../../components/HelpTip.jsx";
import { Badge, Card, ErrorBanner, LoadingBlock, PageHeader, PnlText, StatTile, UnprovenNotice } from "../../components/ui.jsx";
import { GLOSSARY } from "../../glossary.js";
import { closedTrades, isThin, summarize } from "../../lib/stats.js";
import { pnlLabels, quoteCurrency } from "../../pnlLabels.js";

// Practice mode: the bot reads practice-server prices and simulates every trade (no order is sent).
export default function TestnetDashboard({ account, onAccountChanged, onActivate }) {
  const L = pnlLabels(account, { paper: true });
  const unit = quoteCurrency(account) || "USDT";
  const { status, trades, reload } = useTrades(true, account.id);
  const [error, setError] = useState(null);
  const [bypassing, setBypassing] = useState(false);

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
  const closed = useMemo(() => closedTrades(trades), [trades]);
  const stats = useMemo(() => summarize(closed), [closed]);
  const open = trades.filter((t) => t.status === "open");

  return (
    <div className="grid gap-4">
      <PageHeader title="Dashboard" subtitle={`${account.pair} · ${account.market_type}`}>
        <Badge tone="sky">PRACTICE - Simulated trades</Badge>
      </PageHeader>

      <ErrorBanner message={error} />
      <UnprovenNotice paper closedCount={status === "ready" ? stats.n : null} />
      <BotStatusCard account={account} />

      <Card>
        <p className="text-sm font-medium text-fg">
          <span className="mr-1">{GLOSSARY.testnet.term}</span>
          <HelpTip text={GLOSSARY.testnet.long} />
        </p>
        <p className="mt-1 text-sm text-muted">
          Everything on this page is simulated. The bot reads prices from Binance's practice server, then records
          each practice trade itself. No order is sent to Binance, no real money is involved, and nothing here can be
          withdrawn. Real trades can fill at different prices and pay fees.
        </p>
      </Card>

      <dl className="grid grid-cols-2 gap-3 shell:grid-cols-4">
        <StatTile
          label="Practice balance"
          value={`${account.paper_balance.toFixed(2)} ${unit}`}
          help="A simulated balance used only for practice trades. It is not real money, was never deposited by you, and cannot be withdrawn."
        />
        <StatTile label={L.testPnl} value={<PnlText value={status === "ready" ? stats.totalPnl : null} unit={unit} />} />
        <StatTile label="Open test positions" value={status === "ready" ? open.length : "-"} />
        <StatTile
          label={L.winRate}
          value={stats.winRate === null ? "-" : `${stats.winRate.toFixed(0)}%`}
          basis={stats.n && isThin(stats.n) ? "not enough trades to read" : null}
          thin={stats.n > 0 && isThin(stats.n)}
        />
      </dl>
      {L.caption && <p className="-mt-2 text-xs text-muted">{L.caption}</p>}

      {status === "error" && (
        <div>
          <ErrorBanner message="We could not load your practice trades, so the charts are not shown." />
          <button type="button" onClick={reload} className="rounded-ctl border border-line-strong px-4 py-2 text-sm font-medium hover:bg-surface-2">
            Try again
          </button>
        </div>
      )}
      {status === "loading" && <LoadingBlock />}

      {status === "ready" && (
        <div className="grid items-start gap-4 shell:grid-cols-[minmax(0,1.65fr)_minmax(0,1fr)]">
          <div className="grid min-w-0 gap-4">
            <PnlHero closed={closed} L={L} unit={unit} />
            <DailyCard closed={closed} L={L} unit={unit} />
          </div>
          <div className="grid min-w-0 gap-4">
            {open.length === 0 ? (
              <NoPosition />
            ) : (
              open.map((t) => (
                <PositionCard
                  key={t.id}
                  title="Open test position"
                  L={L}
                  unit={unit}
                  position={{ side: t.side, entry_price: t.entry_price, stop_loss_price: t.stop_loss_price, quantity: t.quantity, opened_at: t.opened_at, current_price: null, unrealized_pnl: null }}
                />
              ))
            )}
            <WinLossCard stats={stats} />
          </div>
        </div>
      )}

      {status === "ready" && <RecentTrades closed={closed} L={L} unit={unit} title="Recent test trades" emptyBody="No test trades yet. The bot is monitoring the market for qualifying opportunities." />}

      <UnderstandingTestnet labels={L} />

      <div className="rounded-card border border-line border-l-4 border-l-brand-line bg-surface p-5">
        <p className="text-sm font-semibold text-fg">Ready to move beyond practice trading?</p>
        <p className="mt-1 text-sm text-muted">
          Activating your account unlocks the full trading environment. You'll still choose, separately and
          explicitly, if and when to connect real funds - activating does not do that by itself.
        </p>
        <button
          type="button"
          onClick={onActivate}
          className="mt-4 min-h-[44px] w-full rounded-ctl bg-brand px-5 text-sm font-semibold text-brand-ink hover:brightness-105 sm:w-auto"
        >
          Activate
        </button>
      </div>

      {!account.live_bypass_accepted_at && (
        <div className="flex items-center gap-1 text-xs text-muted">
          <button type="button" onClick={acceptBypass} disabled={bypassing} className="underline hover:text-fg disabled:opacity-60">
            Skip the {daysLeft}-day practice period
          </button>
          <HelpTip text="Lets this account become eligible for live trading sooner, instead of waiting out the full practice period. This changes nothing by itself - you still activate, connect Binance, and enable live trading as separate, deliberate steps." />
        </div>
      )}
    </div>
  );
}

const TOPICS = ["testnet", "pair", "spot", "futures", "position", "pnl"];

function UnderstandingTestnet({ labels }) {
  return (
    <details className="rounded-card border border-line bg-surface p-4">
      <summary className="cursor-pointer text-sm font-semibold text-fg">Understanding Testnet</summary>
      <dl className="mt-3 space-y-3">
        {TOPICS.map((key) => (
          <div key={key}>
            <dt className="text-sm font-medium text-fg">{GLOSSARY[key].term}</dt>
            <dd className="text-sm text-muted">{key === "pnl" ? labels.glossary(GLOSSARY[key].long) : GLOSSARY[key].long}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
