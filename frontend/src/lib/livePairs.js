// Live multi-position: choosing the extra pairs, and the read-only open-risk cap text. The server checks every rule again (src/mba_bot/api/routers/account.py _pair_list_problem).
// Run with: node --test frontend/src/lib/livePairs.test.js (also run by tests/test_frontend_lib.py)
import { usd } from "./format.js";

export const HARD_MAX_SLOTS = 3;

// How many extra pairs fit: one per open trade after the first (the main pair is the first).
export function maxExtraPairs(slots) {
  const n = Number(slots);
  if (!Number.isInteger(n) || n < 2) return 0;
  return Math.min(n, HARD_MAX_SLOTS) - 1;
}

// Tick or untick a pair. A pair is never added twice, the main pair is never added, and nothing is added once the list is full.
export function toggleExtra(list, pair, { slots, primary }) {
  const current = Array.isArray(list) ? list : [];
  if (current.includes(pair)) return current.filter((p) => p !== pair);
  if (pair === primary || current.length >= maxExtraPairs(slots)) return current;
  return [...current, pair];
}

// What Save sends: the chosen pairs kept to what the slots allow, without the main pair. With one open trade the list is empty.
export function listToSave(list, { slots, primary }) {
  const out = [];
  for (const p of Array.isArray(list) ? list : []) if (p !== primary && !out.includes(p)) out.push(p);
  return out.slice(0, maxExtraPairs(slots));
}

export function sameList(a, b) {
  const x = Array.isArray(a) ? a : [];
  const y = Array.isArray(b) ? b : [];
  return x.length === y.length && x.every((p, i) => p === y[i]);
}

// The cap line. Only numbers the service sent or the customer typed; the cap itself is not editable.
export function capText({ capPct, riskPct, slots, capital }) {
  const cap = Number(capPct);
  if (!Number.isFinite(cap) || cap <= 0) return null;
  const cp = Number(capital);
  const value = Number.isFinite(cp) && cp > 0 ? ` (${usd((cp * cap) / 100)})` : "";
  let text = `The service caps the open risk across all your open trades at ${cap}% of your trading capital${value}, and never above your daily loss limit. You cannot change this cap here.`;
  const r = Number(riskPct);
  const n = Number(slots);
  if (Number.isFinite(r) && r > 0 && Number.isInteger(n) && n >= 2) {
    text += ` At ${r}% risk per trade, ${n} trades together risk up to ${Math.round(r * n * 100) / 100}% of your trading capital if every stop is hit.`;
  }
  return text;
}
