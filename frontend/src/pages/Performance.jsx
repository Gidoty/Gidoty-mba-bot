import { useMemo, useState } from "react";

import BarChart from "../components/charts/BarChart.jsx";
import EquityChart from "../components/charts/EquityChart.jsx";
import SplitBar from "../components/charts/SplitBar.jsx";
import useTrades from "../components/dashboard/useTrades.js";
import { Card, CardTitle, EmptyState, ErrorBanner, LoadingBlock, PageHeader, Pill, PnlText, Seg, StatTile, ThinNotice, UnprovenNotice } from "../components/ui.jsx";
import { useAccount } from "../context/AccountContext.jsx";
import { dayMonth, fmt, signed } from "../lib/format.js";
import { closedTrades, cumulativeSeries, filterRange, isThin, monthlyBuckets, summarize } from "../lib/stats.js";
import { pnlLabels, quoteCurrency } from "../pnlLabels.js";

const RANGES = [
  { value: "7", label: "7D" },
  { value: "30", label: "30D" },
  { value: "90", label: "90D" },
  { value: "all", label: "All" },
];

export default function Performance() {
  const { account } = useAccount();
  const isPaper = account.mode !== "live";
  const L = pnlLabels(account, { paper: isPaper });
  const unit = quoteCurrency(account) || "USDT";
  const { status, trades, reload } = useTrades(isPaper, account.id);
  const [range, setRange] = useState("all");
  const days = range === "all" ? null : Number(range);

  const allClosed = useMemo(() => closedTrades(trades), [trades]);
  const inRange = useMemo(() => filterRange(allClosed, days), [allClosed, days]);
  const stats = useMemo(() => summarize(inRange), [inRange]);
  const points = useMemo(() => cumulativeSeries(allClosed, { days }), [allClosed, days]);
  const months = useMemo(() => monthlyBuckets(inRange), [inRange]);
  const thin = isThin(stats.n);

  return (
    <div className="grid gap-4">
      <PageHeader title="Performance" subtitle={isPaper ? "Based on practice trades (simulated)." : "Based on real trades."}>
        <Seg label="Range" value={range} onChange={setRange} options={RANGES} />
      </PageHeader>
      <UnprovenNotice paper={isPaper} closedCount={status === "ready" ? allClosed.length : null} />

      {status === "error" && (
        <div>
          <ErrorBanner message="We could not load your trades, so there is nothing to show." />
          <button type="button" onClick={reload} className="rounded-ctl border border-line-strong px-4 py-2 text-sm font-medium hover:bg-surface-2">
            Try again
          </button>
        </div>
      )}
      {status === "loading" && <LoadingBlock />}

      {status === "ready" && (
        <>
          {stats.n > 0 && thin && <ThinNotice closedCount={stats.n} />}

          <Card>
            <CardTitle right={thin && stats.n > 0 ? <Pill tone="info">Not enough trades to read</Pill> : null}>{L.tag("Statistics")}</CardTitle>
            {stats.n === 0 ? (
              <EmptyState
                title={allClosed.length ? "No closed trades in this range" : "No closed trades yet"}
                body={allClosed.length ? "Choose a longer range." : "Win rate, profit factor and drawdown appear after your first closed trade."}
              />
            ) : (
              <dl className="grid grid-cols-2 gap-3 shell:grid-cols-4">
                <StatTile label={L.winRate} value={`${fmt(stats.winRate, 0)}%`} basis={`${stats.wins} ${stats.wins === 1 ? "win" : "wins"}, ${stats.losses} ${stats.losses === 1 ? "loss" : "losses"}`} thin={thin} />
                <StatTile label="Profit factor" value={stats.profitFactor === null ? "no losses yet" : fmt(stats.profitFactor)} basis="gross wins ÷ gross losses" thin={thin} />
                <StatTile label="Average win" value={stats.avgWin === null ? "none yet" : <PnlText value={stats.avgWin} unit={unit} />} basis={L.basis || null} thin={thin} />
                <StatTile label="Average loss" value={stats.avgLoss === null ? "none yet" : <PnlText value={stats.avgLoss} unit={unit} />} basis={L.basis || null} thin={thin} />
                <StatTile label="Best trade" value={<PnlText value={stats.best.pnl} unit={unit} />} basis={`${dayMonth(stats.best.ms)}${L.basis ? `, ${L.basis}` : ""}`} thin={thin} />
                <StatTile label="Worst trade" value={<PnlText value={stats.worst.pnl} unit={unit} />} basis={`${dayMonth(stats.worst.ms)}${L.basis ? `, ${L.basis}` : ""}`} thin={thin} />
                <StatTile
                  label="Maximum drawdown"
                  value={stats.maxDrawdown ? `-${fmt(stats.maxDrawdown)} ${unit}` : "none yet"}
                  basis={`largest fall of cumulative P&L from a peak${L.basis ? `, ${L.basis}` : ""}`}
                  thin={thin}
                />
                <StatTile label="Net P&L" value={<PnlText value={stats.totalPnl} unit={unit} />} basis={`${stats.n} closed ${stats.n === 1 ? "trade" : "trades"}${L.basis ? `, ${L.basis}` : ""}`} />
              </dl>
            )}
          </Card>

          <div className="grid items-start gap-4 shell:grid-cols-[minmax(0,1.65fr)_minmax(0,1fr)]">
            <Card style={{ backgroundImage: "linear-gradient(180deg, var(--glow), transparent 55%)" }}>
              <CardTitle>{L.cumulative}</CardTitle>
              {points.length ? (
                <>
                  <p className="mb-3 font-mono">
                    <span className={`text-3xl font-medium tracking-tight ${tone(stats.totalPnl)}`}>{signed(stats.totalPnl)}</span>
                    <span className="ml-2 text-sm text-muted">{unit}</span>
                  </p>
                  <EquityChart points={points} unit={unit} label={L.cumulative} />
                </>
              ) : (
                <EmptyState title="No closed trades in this range" body="The curve appears after your first closed trade." />
              )}
            </Card>
            <div className="grid min-w-0 gap-4">
              <Card>
                <CardTitle>Wins and losses</CardTitle>
                {stats.n ? <SplitBar wins={stats.wins} losses={stats.losses} /> : <EmptyState title="No closed trades" body="The split appears after your first closed trade." />}
              </Card>
              <Card>
                <CardTitle>What these figures leave out</CardTitle>
                <p className="text-sm text-muted">
                  Exchange fees and funding are not included, so net results are lower. A high win rate does not mean a profit:{" "}
                  {!thin && stats.avgWin && stats.avgLoss
                    ? `here the average win is ${fmt(stats.avgWin / -stats.avgLoss)} times the average loss.`
                    : "it depends on how large wins are against losses."}
                </p>
              </Card>
            </div>
          </div>

          <Card>
            <CardTitle>{L.tag("P&L by month")}</CardTitle>
            {months.length ? (
              <BarChart items={months} unit={unit} ariaLabel={L.tag(`P&L by month in ${unit}`)} firstColumn="Month" />
            ) : (
              <EmptyState title="No closed trades in this range" body="Monthly bars appear after your first closed trade." />
            )}
          </Card>
        </>
      )}
    </div>
  );
}

const tone = (n) => (n > 0 ? "text-pos" : n < 0 ? "text-neg" : "text-fg");
