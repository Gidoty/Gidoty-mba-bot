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

// ---------------------------------------------------------------------------------------------------- the pop-up: keys, grouping, and what the button shows
// A note is known by its id plus a short hash of its text: the same note with the same words is the same note; a note whose text changed (a number moved) is a new one.
export function textHash(text) {
  let h = 0x811c9dc5; // FNV-1a, 32 bit
  const s = String(text ?? "");
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

export function noteKey(note) {
  return `${String(note?.id ?? "")}:${textHash(note?.text)}`;
}

export const GROUP_ORDER = ["not_possible", "warning", "tip"];

// The notes in the pop-up: grouped by severity in a fixed order, empty groups left out, the server's order kept inside a group.
export function groupNotes(notes) {
  const sorted = sortNotes(notes);
  return GROUP_ORDER.map((severity) => ({ severity, label: SEVERITY_LABEL[severity], notes: sorted.filter((n) => n.severity === severity) })).filter((g) => g.notes.length > 0);
}

// What the one small button does. `read` is a Set of note keys.
//   visible: there is something the customer has not read, OR a "not possible" note exists. That note explains why Save will be refused, so it stays for as long as it exists
//            (it ends when the customer fixes the value and the note goes). If its words change because a number changed, it is a new note and counts as unread again.
//   count:   the unread notes; when everything is read and only "not possible" notes keep the button, those.
//   alert:   at least one "not possible" note exists (the button shows a red dot).
export function buttonState(notes, read) {
  const list = Array.isArray(notes) ? notes : [];
  const known = read instanceof Set ? read : new Set();
  const unread = list.filter((n) => !known.has(noteKey(n)));
  const blockers = list.filter((n) => n.severity === "not_possible");
  return { visible: unread.length > 0 || blockers.length > 0, count: unread.length > 0 ? unread.length : blockers.length, alert: blockers.length > 0 };
}

// ---------------------------------------------------------------------------------------------------- read state: a per-browser convenience
// Only note keys are stored (an id, a colon and 8 hex digits), never a note's text or anything about the customer. Storage can be blocked or full: every access is in try/catch and the same Set is
// kept in memory, so the screen works the same without storage. The list is capped; the oldest keys go first.
export const READ_STORAGE_KEY = "mba_advice_read_v1";
export const READ_MAX = 300;
const KEY_PATTERN = /^[a-z0-9_]{1,40}:[0-9a-f]{8}$/;

export function createReadStore(getStorage = () => globalThis.localStorage) {
  let keys = [];
  const listeners = new Set();
  try {
    const raw = getStorage()?.getItem(READ_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (Array.isArray(parsed)) keys = parsed.filter((k) => typeof k === "string" && KEY_PATTERN.test(k)).slice(-READ_MAX);
  } catch {
    keys = [];
  }
  let snapshot = new Set(keys);
  return {
    snapshot: () => snapshot,
    has: (key) => snapshot.has(key),
    markRead(newKeys) {
      const add = (Array.isArray(newKeys) ? newKeys : []).filter((k) => typeof k === "string" && KEY_PATTERN.test(k) && !snapshot.has(k));
      if (add.length === 0) return;
      keys = [...keys, ...add].slice(-READ_MAX);
      snapshot = new Set(keys);
      try {
        getStorage()?.setItem(READ_STORAGE_KEY, JSON.stringify(keys));
      } catch {
        // blocked or full: the in-memory copy keeps working for this visit
      }
      listeners.forEach((fn) => fn());
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
