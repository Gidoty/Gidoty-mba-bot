import { useEffect, useMemo, useState } from "react";

import { api } from "../api/client.js";
import { Card, EmptyState, LoadingBlock, PageHeader, PnlText, StatTile } from "../components/ui.jsx";
import { useAccount } from "../context/AccountContext.jsx";

const WINDOWS = [
  { key: "pnl_7d", label: "7D", days: 7 },
  { key: "pnl_30d", label: "30D", days: 30 },
  { key: "pnl_90d", label: "90D", days: 90 },
  { key: "net_pnl", label: "ALL", days: null },
];

export default function Performance() {
  const { account } = useAccount();
  const [summary, setSummary] = useState(undefined);
  const [closedTrades, setClosedTrades] = useState(undefined);
  const [windowKey, setWindowKey] = useState("pnl_30d");
  const isPaper = account.mode !== "live";

  useEffect(() => {
    api.get(`/trades/summary?is_paper=${isPaper}`).then(setSummary).catch(() => setSummary(null));
    api
      .get(`/trades?is_paper=${isPaper}`)
      .then((all) => setClosedTrades(all.filter((t) => t.status === "closed" && t.closed_at)))
      .catch(() => setClosedTrades([]));
  }, [account.id, isPaper]);

  const activeWindow = WINDOWS.find((w) => w.key === windowKey);

  const series = useMemo(() => {
    if (!closedTrades) return [];
    const cutoff = activeWindow.days === null ? null : Date.now() - activeWindow.days * 86400000;
    const inWindow = closedTrades
      .filter((t) => (cutoff === null ? true : new Date(t.closed_at).getTime() >= cutoff))
      .sort((a, b) => new Date(a.closed_at) - new Date(b.closed_at));
    let running = 0;
    return inWindow.map((t) => {
      running += t.pnl || 0;
      return { date: t.closed_at, cumulative: running };
    });
  }, [closedTrades, activeWindow]);

  if (summary === undefined || closedTrades === undefined) return <LoadingBlock />;

  return (
    <div>
      <PageHeader title="Performance" subtitle={isPaper ? "Based on practice trades." : "Based on real trades."} />

      <div className="flex gap-1 rounded-lg bg-slate-100 p-1">
        {WINDOWS.map((w) => (
          <button
            key={w.key}
            onClick={() => setWindowKey(w.key)}
            className={`flex-1 rounded-md py-1.5 text-sm font-medium ${
              windowKey === w.key ? "bg-white text-ink shadow-sm" : "text-slate-500"
            }`}
          >
            {w.label}
          </button>
        ))}
      </div>

      <div className="mt-4 grid grid-cols-3 gap-3">
        <StatTile label={`P&L (${activeWindow.label})`} value={<PnlText value={summary[windowKey]} />} />
        <StatTile label="Total Trades" value={summary.total_trades} />
        <StatTile label="Win Rate" value={summary.win_rate === null ? "-" : `${(summary.win_rate * 100).toFixed(0)}%`} />
      </div>

      <h2 className="mt-6 mb-2 text-sm font-semibold text-slate-700">Cumulative P&L</h2>
      {series.length === 0 ? (
        <EmptyState
          title="Not enough data yet"
          body="A performance chart will appear here once some trades have closed in this period."
        />
      ) : (
        <Card>
          <CumulativePnlChart series={series} />
        </Card>
      )}
    </div>
  );
}

// A small inline SVG line chart - built directly from each real closed
// trade's own pnl/closed_at, never an invented or interpolated series.
function CumulativePnlChart({ series }) {
  const width = 320;
  const height = 120;
  const values = series.map((p) => p.cumulative);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;
  const points = series.map((p, i) => {
    const x = series.length === 1 ? width : (i / (series.length - 1)) * width;
    const y = height - ((p.cumulative - min) / span) * height;
    return `${x},${y}`;
  });
  const zeroY = height - ((0 - min) / span) * height;
  const last = values[values.length - 1];
  const stroke = last >= 0 ? "#16a34a" : "#dc2626";

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-32 w-full" preserveAspectRatio="none">
      <line x1="0" y1={zeroY} x2={width} y2={zeroY} stroke="#e2e8f0" strokeWidth="1" />
      <polyline points={points.join(" ")} fill="none" stroke={stroke} strokeWidth="2" />
    </svg>
  );
}
