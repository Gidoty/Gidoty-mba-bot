import { dayMonthTime, fmt, signed } from "../../lib/format.js";
import { fmtPrice, positionView } from "../../lib/position.js";
import { Card, CardTitle, EmptyState, Pill } from "../ui.jsx";

// One open position with the distance to its stop. `position` is the API shape of GET /account/position
// (entry_price, current_price, stop_loss_price, quantity, side, unrealized_pnl, protected, opened_at).
// current_price and unrealized_pnl may be missing (practice trades have no live price); the card then shows what it has.
export default function PositionCard({ position, L, unit, title = "Open position", badge = null }) {
  const { entry_price: entry, stop_loss_price: stop, current_price: now, quantity: qty, unrealized_pnl: unreal } = position;
  const v = positionView(position);
  const { long, decimals } = v;
  const profitLocked = v.pnlIfStopped !== null && v.pnlIfStopped > 0;

  return (
    <Card>
      <CardTitle
        right={
          <span className="flex flex-wrap items-center gap-2">
            {badge}
            <Pill tone={long ? "ok" : "bad"}>{long ? "Long" : "Short"}</Pill>
          </span>
        }
      >
        {title}
      </CardTitle>
      <p className="mb-1 font-mono">
        <span className={`text-3xl font-medium tracking-tight ${unreal > 0 ? "text-pos" : unreal < 0 ? "text-neg" : "text-muted"}`}>
          {unreal === null || unreal === undefined ? "-" : signed(unreal)}
        </span>
        <span className="ml-2 text-sm text-muted">{unit}</span>
      </p>
      {L.unrealized && unreal !== null && unreal !== undefined && <p className="text-sm text-muted">{L.unrealized}</p>}

      <Gauge long={long} entry={entry} stop={stop} now={now} decimals={decimals} />

      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
        <Item label="Stop distance">
          {v.distance === null ? (
            "-"
          ) : (
            <>
              <span className="font-mono">{fmtPrice(v.distance, decimals)} {unit}</span>
              <span className="block text-muted">{fmt(v.distancePct)}% {v.stopSide} price</span>
            </>
          )}
        </Item>
        <Item label={`${profitLocked ? "Result if stopped" : "Loss if stopped"}${L.on ? ", before fees" : ""}`}>
          {v.pnlIfStopped === null ? "-" : (
            <span className={`font-mono ${v.pnlIfStopped > 0 ? "text-pos" : v.pnlIfStopped < 0 ? "text-neg" : "text-muted"}`}>
              {signed(v.pnlIfStopped)} {unit}
            </span>
          )}
        </Item>
        <Item label="Quantity">
          <span className="font-mono">{qty ?? "-"}</span>
        </Item>
        <Item label="Opened (UTC)">
          <span className="font-mono">{position.opened_at ? dayMonthTime(new Date(position.opened_at).getTime()) : "-"}</span>
        </Item>
      </dl>
      {v.beyondStop && <p role="alert" className="mt-3 text-sm text-neg">The price is beyond the stop. The bot is handling it; check your Binance account.</p>}
      {position.protected === true && (
        <p className="mt-3 flex items-center gap-2 text-sm text-muted">
          <Pill tone="ok">Protected</Pill> The stop-loss is resting at Binance.
        </p>
      )}
      {position.protected === false && (
        <p className="mt-3 flex items-center gap-2 text-sm text-muted">
          <Pill tone="warn">Checking</Pill> Setting up or checking the stop-loss at Binance.
        </p>
      )}
    </Card>
  );
}

function Item({ label, children }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5">{children}</dd>
    </div>
  );
}

// Stop, entry and current price on one line. Close markers move their labels to a second row so text never overlaps.
function Gauge({ long, entry, stop, now, decimals }) {
  const marks = [
    stop != null && { key: "stop", label: "Stop", value: stop },
    entry != null && { key: "entry", label: "Entry", value: entry },
    now != null && { key: "now", label: "Now", value: now },
  ].filter(Boolean);
  if (marks.length < 2) return null;
  const vals = marks.map((m) => m.value);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const pad = (hi - lo) * 0.12 || 1;
  const pct = (v) => ((v - (lo - pad)) / (hi - lo + 2 * pad)) * 100;
  const sorted = [...marks].sort((a, b) => a.value - b.value).map((m) => ({ ...m, p: pct(m.value) }));
  let prev = -100;
  let row = 0;
  sorted.forEach((m) => {
    row = m.p - prev < 22 ? 1 - row : 0;
    m.row = row;
    prev = m.p;
  });
  const danger = stop != null && entry != null ? [Math.min(pct(stop), pct(entry)), Math.max(pct(stop), pct(entry))] : null;
  const gain = entry != null && now != null ? [Math.min(pct(entry), pct(now)), Math.max(pct(entry), pct(now))] : null;
  const winning = entry != null && now != null ? (long ? now >= entry : now <= entry) : true;

  return (
    <div className="relative mx-2 mb-12 mt-5 h-10" role="img" aria-label={`Stop ${stop ?? "-"}, entry ${entry ?? "-"}, price now ${now ?? "-"}`}>
      <div className="absolute inset-x-0 top-3 h-2 rounded-full border border-line bg-surface-2" />
      {danger && <div className="absolute top-3 h-2 bg-neg-soft" style={{ left: `${danger[0]}%`, width: `${danger[1] - danger[0]}%` }} />}
      {gain && <div className={`absolute top-3 h-2 ${winning ? "bg-pos-bar" : "bg-neg-bar"}`} style={{ left: `${gain[0]}%`, width: `${gain[1] - gain[0]}%`, opacity: 0.85 }} />}
      {sorted.map((m) => (
        <div key={m.key} className="absolute top-0 -translate-x-1/2" style={{ left: `${m.p}%` }}>
          <div className={`mx-auto h-8 w-[3px] rounded ${m.key === "stop" ? "bg-neg-bar" : m.key === "now" ? "bg-brand-line" : "bg-fg"}`} />
          <div className="whitespace-nowrap text-center text-xs leading-tight" style={{ marginTop: m.row ? 22 : 2 }}>
            <span className="text-muted">{m.label}</span>
            <b className="block font-mono font-medium text-fg">{fmtPrice(m.value, decimals)}</b>
          </div>
        </div>
      ))}
    </div>
  );
}

export function NoPosition() {
  return (
    <Card>
      <CardTitle>Open position</CardTitle>
      <EmptyState title="No open position" body="The bot is monitoring the market for qualifying opportunities." />
    </Card>
  );
}
