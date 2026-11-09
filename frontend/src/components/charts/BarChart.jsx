import { useRef } from "react";

import { signed } from "../../lib/format.js";
import ChartFallback from "./ChartFallback.jsx";
import { niceTicks } from "./scale.js";
import useWidth from "./useWidth.js";

// Bars above and below a zero line: a gain sits above, a loss below, and each
// bar that has room carries its signed value. Colour is never the only signal.
// `items` come from lib/stats.js: [{ key, label, value, count }].
export default function BarChart({ items, unit = "USDT", ariaLabel, firstColumn = "Period", height = 220 }) {
  const box = useRef(null);
  const W = useWidth(box);
  const H = height;
  const L = W < 480 ? 46 : 56;
  const R = 10;
  const T = 22;
  const B = 28;

  const values = items.map((i) => i.value);
  const { ticks, lo, hi } = niceTicks(Math.min(0, ...values), Math.max(0, ...values), 4);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const slot = (W - L - R) / items.length;
  const barW = Math.min(40, slot * 0.62);
  const showValues = slot >= 34;
  const every = Math.max(1, Math.ceil(30 / slot)); // thin out axis labels when bars are narrow

  return (
    <figure ref={box} className="grid gap-2">
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={ariaLabel} className="block max-w-full overflow-visible">
        {ticks.map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke={v === 0 ? "var(--muted)" : "var(--line)"} />
            <text x={L - 8} y={y(v) + 4} textAnchor="end" fontSize="11" className="font-mono" fill="var(--muted)">
              {v === 0 ? "0" : signed(v, 0)}
            </text>
          </g>
        ))}
        {items.map((it, i) => {
          const cx = L + slot * (i + 0.5);
          const y0 = y(0);
          const y1 = y(it.value);
          const top = Math.min(y0, y1);
          const h = Math.max(1.5, Math.abs(y1 - y0));
          const positive = it.value > 0;
          return (
            <g key={it.key}>
              {it.value !== 0 && (
                <rect x={cx - barW / 2} y={top} width={barW} height={h} rx="3" fill={positive ? "var(--pos-bar)" : "var(--neg-bar)"} />
              )}
              {it.value !== 0 && showValues && (
                <text
                  x={cx}
                  y={positive ? top - 5 : top + h + 13}
                  textAnchor="middle"
                  fontSize="11"
                  fontWeight="600"
                  className="font-mono"
                  fill={positive ? "var(--pos)" : "var(--neg)"}
                >
                  {signed(it.value, 0)}
                </text>
              )}
              {i % every === 0 && (
                <text x={cx} y={H - 8} textAnchor="middle" fontSize="11" className="font-mono" fill="var(--muted)">
                  {it.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <ChartFallback
        columns={[firstColumn, `P&L, ${unit}, before fees`, "Trades"]}
        rows={items.map((it) => [it.label, signed(it.value), String(it.count)])}
      />
    </figure>
  );
}
