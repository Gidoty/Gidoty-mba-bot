import { useEffect, useMemo, useState } from "react";

import { api } from "../../api/client.js";
import BotStatusCard from "../../components/BotStatusCard.jsx";
import { DailyCard, PnlHero, WinLossCard } from "../../components/dashboard/PerformancePanels.jsx";
import PositionCard, { NoPosition } from "../../components/dashboard/PositionCard.jsx";
import RecentTrades from "../../components/dashboard/RecentTrades.jsx";
import useTrades from "../../components/dashboard/useTrades.js";
import { Badge, Card, CardTitle, ErrorBanner, LoadingBlock, PageHeader, PnlText, StatTile, UnprovenNotice } from "../../components/ui.jsx";
import { closedTrades, isThin, summarize } from "../../lib/stats.js";
import { pnlLabels, quoteCurrency } from "../../pnlLabels.js";

export default function LiveDashboard({ account }) {
  const L = pnlLabels(account, { paper: false });
  const unit = quoteCurrency(account) || "USDT";
  const [balance, setBalance] = useState(undefined); // undefined = loading, null = unavailable
  const [summary, setSummary] = useState(null);
  const [position, setPosition] = useState(undefined);
  const { status, trades, reload } = useTrades(false, account.id);

  useEffect(() => {
    api.get("/account/balance").then(setBalance).catch(() => setBalance(null));
    api.get("/trades/summary?is_paper=false").then(setSummary).catch(() => {});
    api.get("/account/position").then(setPosition).catch(() => setPosition(null));
  }, [account.id]);

  const closed = useMemo(() => closedTrades(trades), [trades]);
  const stats = useMemo(() => summarize(closed), [closed]);
  const thin = isThin(stats.n);

  return (
    <div className="grid gap-4">
      <PageHeader title="Dashboard" subtitle={`${account.pair} · ${account.market_type}`}>
        <Badge tone="red">LIVE</Badge>
      </PageHeader>

      <UnprovenNotice closedCount={status === "ready" ? stats.n : null} />
      <BotStatusCard account={account} />

      <dl className="grid grid-cols-2 gap-3 shell:grid-cols-4">
        <StatTile
          label="Exchange balance"
          value={balance === undefined ? "..." : balance === null ? "Unavailable" : `${balance.available_balance.toFixed(2)} ${balance.quote_currency || unit}`}
          help="The free balance in the quote currency of your trading pair, read directly from your Binance account."
        />
        <StatTile label="Today" value={<PnlText value={summary?.pnl_today ?? null} unit={unit} />} basis={L.basis || null} />
        <StatTile label="7D" value={<PnlText value={summary?.pnl_7d ?? null} unit={unit} />} basis={L.basis || null} />
        <StatTile label="30D" value={<PnlText value={summary?.pnl_30d ?? null} unit={unit} />} basis={L.basis || null} />
        <StatTile label={L.allTime} value={<PnlText value={summary?.net_pnl ?? null} unit={unit} />} />
        <StatTile label="Total trades" value={summary?.total_trades ?? "-"} />
        <StatTile
          label={L.winRate}
          value={stats.winRate === null ? "-" : `${stats.winRate.toFixed(0)}%`}
          basis={stats.n && thin ? "not enough trades to read" : stats.n ? `${stats.wins} wins, ${stats.losses} losses` : null}
          thin={stats.n > 0 && thin}
        />
      </dl>
      {L.caption && <p className="-mt-2 text-xs text-muted">{L.caption}</p>}

      {status === "error" && (
        <div>
          <ErrorBanner message="We could not load your trades, so the charts are not shown." />
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
            {position === undefined ? (
              <Card>
                <CardTitle>Open position</CardTitle>
                <LoadingBlock />
              </Card>
            ) : !position || !position.has_position ? (
              <NoPosition />
            ) : (
              <PositionCard position={position} L={L} unit={unit} />
            )}
            <WinLossCard stats={stats} />
          </div>
        </div>
      )}

      {status === "ready" && <RecentTrades closed={closed} L={L} unit={unit} />}
    </div>
  );
}
