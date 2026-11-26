// Admin page helpers (the server side is src/mba_bot/api/routers/admin.py). Pure functions only. Nothing here holds a credential, a network address or an email.
// Run with: node --test frontend/src/lib/admin.test.js (also run by tests/test_frontend_lib.py)
export const REASON_MIN = 5;
export const REASON_MAX = 200;
export const REFRESH_MS = 60_000;
export const WINDOWS = [
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "0", label: "All time" },
];

// What the screen shows for GET /api/admin/me: nothing, the sign-in step, or the dashboard. Any error or a missing answer is "hidden".
export function adminStage(me) {
  if (!me || me.is_admin !== true) return "hidden";
  return me.session === true ? "dashboard" : "login";
}

// The code box takes 6 digits or a recovery code (16 letters and digits, typed with or without dashes). The server decides; this only stops an empty or absurd box.
export function codeProblem(raw) {
  const v = String(raw ?? "").replace(/[\s-]/g, "");
  if (!v) return "Enter the 6-digit code from your authenticator app, or a recovery code.";
  if (/^\d+$/.test(v) && v.length !== 6) return "The code from your authenticator app has 6 digits.";
  if (!/^[A-Za-z0-9]+$/.test(v) || v.length > 16) return "That does not look like a code.";
  return null;
}

export function reasonProblem(raw) {
  const n = String(raw ?? "").trim().length;
  if (n < REASON_MIN || n > REASON_MAX) return `Give a reason of ${REASON_MIN} to ${REASON_MAX} characters.`;
  return null;
}

export function ageText(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "never";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

export const MODE_TEXT = { practice: "Practice", live: "Live", live_stopped: "Live, stopped", inactive: "Inactive" };
export const KILL_SWITCH_TEXT = { released: "Released (trading allowed)", engaged: "ENGAGED (new live trades paused)", missing: "Missing (treated as engaged)", malformed: "Unreadable (treated as engaged)", error: "Could not read (treated as engaged)" };

export function killSwitchTone(state) {
  return state === "released" ? "ok" : "bad";
}

// A result figure exactly as the server labelled it: the number, then the server's own wording. A net number is shown only when the server sent one.
export function resultLine(g, unit = "USDT") {
  const n = (x) => `${x >= 0 ? "+" : ""}${Number(x).toFixed(2)}`;
  if (g.simulated) return `${n(g.gross_pnl)} ${unit} (simulated, no fees or slippage)`;
  if (g.net_pnl !== null && g.net_pnl !== undefined) {
    const withFunding = g.funding_complete === true && g.funding !== null && g.funding !== undefined;
    return withFunding ? `${n(g.net_pnl)} ${unit} net after fees and funding (gross ${n(g.gross_pnl)}, funding ${n(g.funding)})` : `${n(g.net_pnl)} ${unit} net after fees (${n(g.gross_pnl)} before fees)`;
  }
  if (g.fees_complete && g.funding_complete === false) return `${n(g.gross_pnl)} ${unit} gross, after fees, before funding`;
  return `${n(g.gross_pnl)} ${unit} before fees`;
}

// The give-back figures exactly as the server counted them: trades whose best price reached +1 R and closed below the entry net of (assumed) costs, and trades that closed more than 1 R below their best price.
// Empty when no closed trade in the group has a recorded best price (older trades have none).
export function giveBackLine(g) {
  if (!g || !g.trades_with_peak) return "";
  const rate = g.give_back_rate === null || g.give_back_rate === undefined ? "-" : `${Math.round(g.give_back_rate * 100)}%`;
  return `Give-back: ${g.gave_back_to_loss || 0} of ${g.reached_1r || 0} trades that reached +1R closed below entry net of costs (${rate}); ${g.closed_far_below_peak || 0} closed more than 1R below their best price (${g.trades_with_peak} trades with a recorded best price)`;
}

export function winRate(g) {
  const d = (g.wins || 0) + (g.losses || 0);
  return d > 0 ? Math.round((g.wins / d) * 100) : null;
}

export function pageCount(total, pageSize) {
  return Math.max(1, Math.ceil((total || 0) / (pageSize || 50)));
}

export function minutesLeft(msSince, ttl = REFRESH_MS) {
  return Math.max(0, Math.ceil((ttl - msSince) / 1000));
}
