// Setup advisor helpers (the server module is src/mba_bot/setup_advice.py). Pure functions: what to ask the server while the customer edits, and which notes to show where.
// Run with: node --test frontend/src/lib/advice.test.js (also run by tests/test_frontend_lib.py)
export const MAX_DASHBOARD_NOTES = 3;
const ORDER = { not_possible: 0, warning: 1, tip: 2 };

function num(raw) {
  if (raw === "" || raw === null || raw === undefined) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

// The values the customer is about to save, in the shape POST /account/advice takes. Only what Save would send: a field left as saved is omitted ("unchanged"), exactly as for PATCH.
// A risk setting that is not a number, and an empty or non-positive amount, are left out (the screen has its own message for them).
export function buildRequest({ account, pair, marketType, riskPct, pairSelectionMode, maxLeverage, capitalInput, limitInput, liveSlots, acks, savedCapital, savedLimit }) {
  const body = {};
  const risk = num(riskPct);
  if (risk !== null) body.risk_pct = risk;
  if (acks?.high) body.acknowledge_high_risk = true;
  if (acks?.very_high) body.acknowledge_very_high_risk = true;
  if (pair !== account.pair) body.pair = pair;
  if (marketType !== account.market_type) body.market_type = marketType;
  if (pairSelectionMode !== account.pair_selection_mode) body.pair_selection_mode = pairSelectionMode;
  const cap = num(capitalInput);
  if (capitalInput !== savedCapital && cap !== null && cap > 0) body.allocated_capital = cap;
  const limit = num(limitInput);
  if (limitInput !== savedLimit && limit !== null && limit > 0) body.daily_loss_limit_usd = limit;
  if (account.live_multi_available && String(liveSlots) !== String(account.live_slots || 1)) body.live_slots = Number(liveSlots);
  const lev = num(maxLeverage);
  if (marketType === "futures" && lev !== null && String(maxLeverage) !== String(account.max_leverage || 3)) body.max_leverage = lev;
  return body;
}

// One stable string per request, so an unchanged form never asks again.
export function requestKey(body) {
  return JSON.stringify(Object.keys(body).sort().map((k) => [k, body[k]]));
}

export function sortNotes(notes) {
  return (Array.isArray(notes) ? notes : []).map((n, i) => [n, i]).sort((a, b) => (ORDER[a[0].severity] ?? 3) - (ORDER[b[0].severity] ?? 3) || a[1] - b[1]).map(([n]) => n);
}

export function notesForTab(notes, tab) {
  return sortNotes(notes).filter((n) => n.tab === tab);
}

// The dashboard shows only what needs attention: "not possible" and warnings, at most three.
export function dashboardNotes(notes) {
  return sortNotes(notes).filter((n) => n.severity === "not_possible" || n.severity === "warning").slice(0, MAX_DASHBOARD_NOTES);
}

export const SEVERITY_LABEL = { not_possible: "Not possible", warning: "Check", tip: "Good to know" };
export const SEVERITY_EDGE = { not_possible: "border-l-neg bg-neg-soft", warning: "border-l-warn bg-warn-soft", tip: "border-l-info bg-info-soft" };
