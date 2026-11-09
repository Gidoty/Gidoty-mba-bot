import { useRef } from "react";

import { dayMonth, dayMonthTime, signed } from "../../lib/format.js";
import ChartFallback from "./ChartFallback.jsx";
import { niceTicks } from "./scale.js";
import useWidth from "./useWidth.js";

// Cumulative P&L line (before fees) built from the trades the API returns.
// `points` come from lib/stats.js cumulativeSeries: [{ t (ms), v (running P&L), pnl (that trade, null for the start point) }].
// The zero line is always drawn. The caller supplies the heading and the unit.
export default function EquityChart({ points, unit = "USDT", height = 240, label = "Cumulative P&L, before fees" }) {
  const box = useRef(null);
  const W = useWidth(box);
  const compact = W < 480;
  const H = compact ? 220 : height;
  const L = compact ? 52 : 64;
  const R = 14;
  const T = 14;
  const B = 28;

  const values = points.map((p) => p.v);
  const { ticks, lo, hi } = niceTicks(Math.min(0, ...values), Math.max(0, ...values), 4);
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const x = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const line = points.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const last = points[points.length - 1];
  const rows = points
    .filter((p) => p.pnl !== null)
    .map((p) => [dayMonthTime(p.t), `${signed(p.pnl)}`, `${signed(p.v)}`]);

  return (
    <figure ref={box} className="grid gap-2">
      <svg
        width={W}
        height={H}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`${label}: ${signed(last.v)} ${unit} after ${rows.length} closed ${rows.length === 1 ? "trade" : "trades"}`}
        className="block max-w-full overflow-visible"
      >
        {ticks.map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke={v === 0 ? "var(--muted)" : "var(--line)"} />
            <text x={L - 8} y={y(v) + 4} textAnchor="end" fontSize="11" className="font-mono" fill="var(--muted)">
              {v === 0 ? "0" : signed(v, 0)}
            </text>
          </g>
        ))}
        <text x={L} y={H - 8} fontSize="11" className="font-mono" fill="var(--muted)">
          {dayMonth(t0)}
        </text>
        <text x={W - R} y={H - 8} textAnchor="end" fontSize="11" className="font-mono" fill="var(--muted)">
          {dayMonth(t1)}
        </text>
        <polygon points={`${x(t0)},${y(0)} ${line} ${x(t1)},${y(0)}`} fill="var(--accent-line)" opacity="0.13" />
        <polyline points={line} fill="none" stroke="var(--accent-line)" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
        <circle cx={x(last.t)} cy={y(last.v)} r="5" fill="var(--accent-line)" stroke="var(--surface)" strokeWidth="2" />
      </svg>
      <ChartFallback
        summary={`View as table (${rows.length} ${rows.length === 1 ? "trade" : "trades"})`}
        columns={["Closed (UTC)", `Trade P&L, ${unit}, before fees`, `Cumulative, ${unit}`]}
        rows={rows}
      />
    </figure>
  );
}

