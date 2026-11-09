// Pure statistics over the trades the existing API already returns
// (GET /trades: closed_at, pnl, status). No balance is used or assumed:
// the app does not know the account balance, so every figure is P&L, and
// P&L is gross (before exchange fees and funding).
import { monthName } from "./format.js";

// Fewer closed trades than this is marked "not enough trades to read".
// Placeholder threshold, to be set by the owner.
export const THIN_THRESHOLD = 9;
const DAY = 86400000;

export function isThin(n) {
  return n < THIN_THRESHOLD;
}

// Closed trades with a result, oldest first.
export function closedTrades(all) {
  return (all || [])
    .filter((t) => t.status === "closed" && t.closed_at && t.pnl !== null && t.pnl !== undefined)
    .map((t) => ({ ...t, ms: new Date(t.closed_at).getTime() }))
    .sort((a, b) => a.ms - b.ms);
}

// Trades closed within the last `days` days (null = all time).
export function filterRange(closed, days, now = Date.now()) {
  return days === null ? closed : closed.filter((t) => t.ms >= now - days * DAY);
}

// Cumulative P&L, starting from 0 at the start of the range. `days` null = all time.
export function cumulativeSeries(closed, { days = null, now = Date.now() } = {}) {
  const inRange = days === null ? closed : closed.filter((t) => t.ms >= now - days * DAY);
  if (!inRange.length) return [];
  const points = [{ t: inRange[0].ms - DAY / 24, v: 0, pnl: null }];
  let running = 0;
  for (const t of inRange) {
    running += t.pnl;
    points.push({ t: t.ms, v: running, pnl: t.pnl });
  }
  return points;
}

// One bucket per UTC day for the `days` days up to and including `endMs`'s day. Days without a trade are 0.
export function dailyBuckets(closed, { days = 14, endMs = Date.now() } = {}) {
  const end = new Date(endMs);
  const endDay = Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate());
  const out = [];
  for (let k = days - 1; k >= 0; k--) {
    const start = endDay - k * DAY;
    const inDay = closed.filter((t) => t.ms >= start && t.ms < start + DAY);
    out.push({
      key: new Date(start).toISOString().slice(0, 10),
      label: String(new Date(start).getUTCDate()),
      value: inDay.reduce((s, t) => s + t.pnl, 0),
      count: inDay.length,
    });
  }
  return out;
}

export function monthlyBuckets(closed) {
  const map = new Map();
  for (const t of closed) {
    const d = new Date(t.ms);
    const key = d.getUTCFullYear() * 12 + d.getUTCMonth();
    const bucket = map.get(key) || { key: String(key), label: monthName(d.getUTCMonth()), value: 0, count: 0 };
    bucket.value += t.pnl;
    bucket.count += 1;
    map.set(key, bucket);
  }
  return [...map.entries()].sort((a, b) => a[0] - b[0]).map(([, v]) => v);
}

export function summarize(closed) {
  const wins = closed.filter((t) => t.pnl > 0);
  const losses = closed.filter((t) => t.pnl < 0);
  const grossWin = wins.reduce((s, t) => s + t.pnl, 0);
  const grossLoss = losses.reduce((s, t) => s + t.pnl, 0);
  let running = 0;
  let peak = 0;
  let maxDrawdown = 0;
  for (const t of closed) {
    running += t.pnl;
    if (running > peak) peak = running;
    if (peak - running > maxDrawdown) maxDrawdown = peak - running;
  }
  const byPnl = [...closed].sort((a, b) => a.pnl - b.pnl);
  return {
    n: closed.length,
    wins: wins.length,
    losses: losses.length,
    totalPnl: grossWin + grossLoss,
    winRate: closed.length ? (wins.length / closed.length) * 100 : null,
    avgWin: wins.length ? grossWin / wins.length : null,
    avgLoss: losses.length ? grossLoss / losses.length : null,
    best: byPnl.length ? byPnl[byPnl.length - 1] : null,
    worst: byPnl.length ? byPnl[0] : null,
    // null when there are no losses yet: "no losses yet", never infinity.
    profitFactor: grossLoss < 0 ? grossWin / -grossLoss : null,
    // Largest fall of cumulative P&L from its earlier peak, in the quote currency. Not a % of the balance: the balance is not known here.
    maxDrawdown,
  };
}
