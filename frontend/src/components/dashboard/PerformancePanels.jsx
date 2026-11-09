import { useMemo, useState } from "react";

import { dailyBuckets, cumulativeSeries, isThin } from "../../lib/stats.js";
import { signed } from "../../lib/format.js";
import BarChart from "../charts/BarChart.jsx";
import EquityChart from "../charts/EquityChart.jsx";
import SplitBar from "../charts/SplitBar.jsx";
import { Card, CardTitle, EmptyState, Seg, ThinNotice } from "../ui.jsx";

const RANGES = [
  { value: "30", label: "30D" },
  { value: "90", label: "90D" },
  { value: "all", label: "All" },
];

// Cumulative P&L for the chosen range: the number, the curve, and a table behind it.
// `closed` comes from lib/stats.js closedTrades(). `now` is injectable for tests.
export function PnlHero({ closed, L, unit, now = Date.now() }) {
  const [range, setRange] = useState("all");
  const days = range === "all" ? null : Number(range);
  const points = useMemo(() => cumulativeSeries(closed, { days, now }), [closed, days, now]);
  const last = points.length ? points[points.length - 1].v : 0;
  const count = Math.max(0, points.length - 1);

  return (
    <Card style={{ backgroundImage: "linear-gradient(180deg, var(--glow), transparent 55%)" }}>
      <CardTitle right={<Seg label="Range" value={range} onChange={setRange} options={RANGES} />}>{L.cumulative}</CardTitle>
      <p className="mb-3 flex flex-wrap items-baseline gap-x-3 font-mono">
        <span className={`text-3xl font-medium tracking-tight shell:text-4xl ${last > 0 ? "text-pos" : last < 0 ? "text-neg" : "text-fg"}`}>
          {signed(last)}
        </span>
        <span className="text-sm text-muted">{unit}</span>
        <span className="font-sans text-sm text-muted">
          {count} closed {count === 1 ? "trade" : "trades"} in range{L.basis ? `, ${L.basis}` : ""}
        </span>
      </p>
      {points.length ? (
        <EquityChart points={points} unit={unit} label={L.cumulative} />
      ) : (
        <EmptyState
          title={closed.length ? "No closed trades in this range" : "No closed trades yet"}
          body={closed.length ? "Choose a longer range." : "The curve appears after your first closed trade."}
        />
      )}
    </Card>
  );
}

export function DailyCard({ closed, L, unit, now = Date.now() }) {
  const endMs = closed.length ? closed[closed.length - 1].ms : now;
  const items = useMemo(() => dailyBuckets(closed, { days: 14, endMs }), [closed, endMs]);
  return (
    <Card>
      <CardTitle>{L.tag("Daily P&L, last 14 days (UTC)")}</CardTitle>
      {closed.length ? (
        <BarChart items={items} unit={unit} ariaLabel={L.tag(`Daily P&L in ${unit} for the 14 days to the last closed trade`)} firstColumn="Day" />
      ) : (
        <EmptyState title="No closed trades yet" body="Daily bars appear after your first closed trade." />
      )}
    </Card>
  );
}

export function WinLossCard({ stats }) {
  return (
    <Card>
      <CardTitle>Wins and losses</CardTitle>
      {stats.n ? (
        <>
          <SplitBar wins={stats.wins} losses={stats.losses} />
          <p className="mt-2 text-sm text-muted">
            {isThin(stats.n) ? "Not enough trades to read a win rate." : `Win rate ${stats.winRate.toFixed(0)}%.`}
          </p>
        </>
      ) : (
        <EmptyState title="No closed trades yet" body="The win and loss split appears after your first closed trade." />
      )}
      {stats.n > 0 && isThin(stats.n) && (
        <div className="mt-3">
          <ThinNotice closedCount={stats.n} />
        </div>
      )}
    </Card>
  );
}
