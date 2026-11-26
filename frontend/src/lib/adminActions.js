// Admin write actions (the server side is src/mba_bot/api/routers/admin.py, docs/safety/operator_settings.md). Pure functions only. Every call carries a reason and a fresh authenticator code; the server decides,
// this only builds the request and stops an empty or absurd box. Run with: node --test frontend/src/lib/adminActions.test.js (also run by tests/test_frontend_admin_actions.py)
import { REASON_MAX, REASON_MIN, codeProblem, reasonProblem } from "./admin.js";

export const ACTIONS = [
  { id: "pause", label: "Pause the bot (no new entries; open trades stay managed)", target: "ref", suffix: "pause" },
  { id: "resume", label: "Resume the bot", target: "ref", suffix: "resume" },
  { id: "suspend", label: "Suspend the account (no sign-in, bot paused)", target: "ref", suffix: "suspend" },
  { id: "reinstate", label: "Reinstate the account (the bot stays paused)", target: "ref", suffix: "reinstate" },
  { id: "extend", label: "Extend the subscription by days", target: "ref", suffix: "subscription/extend", days: true },
  { id: "end", label: "End the subscription now", target: "ref", suffix: "subscription/end" },
  { id: "note", label: "Add a note", target: "ref", suffix: "note", note: true },
  { id: "approve", label: "Approve a pending request", target: "request", suffix: "approve" },
  { id: "refuse", label: "Refuse a pending request", target: "request", suffix: "refuse" },
  { id: "complete_deletion", label: "Complete an approved deletion (erases the customer's data)", target: "request", suffix: "complete-deletion", confirm: true },
  { id: "kill_engage", label: "Engage the kill flag (no new live entries)", target: "none", path: "/admin/kill-switch/engage" },
  { id: "kill_release", label: "Release the kill flag", target: "none", path: "/admin/kill-switch/release" },
];

const byId = Object.fromEntries(ACTIONS.map((a) => [a.id, a]));
const SAFE_ID = /^[A-Za-z0-9-]{1,40}$/;

// {path, body} for one action, or {error}. The target is an account ref or a request id (letters, digits, dashes only, so it can never change the path).
export function buildRequest(actionId, form) {
  const a = byId[actionId];
  if (!a) return { error: "Choose an action." };
  const reason = reasonProblem(form.reason);
  if (reason) return { error: reason };
  const code = codeProblem(form.code);
  if (code) return { error: code };
  const body = { reason: String(form.reason).trim(), code: String(form.code).trim() };
  let path = a.path;
  if (a.target !== "none") {
    const t = String(form.target ?? "").trim();
    if (!SAFE_ID.test(t)) return { error: "Paste the customer ref or request id (letters, digits and dashes)." };
    path = a.target === "ref" ? `/admin/customers/${t}/${a.suffix}` : `/admin/requests/${t}/${a.suffix}`;
  }
  if (a.days) {
    const d = Number(form.days);
    if (!Number.isInteger(d) || d < 1 || d > 366) return { error: "Extend by 1 to 366 days." };
    body.days = d;
  }
  if (a.confirm) {
    const c = String(form.confirm ?? "").trim();
    if (!/^DELETE [A-Za-z0-9-]{8}$/.test(c)) return { error: "Type the confirmation exactly as DELETE and the first 8 characters of the customer id." };
    body.confirm = c;
  }
  if (a.note) {
    const n = String(form.note ?? "").trim();
    if (n.length < 1 || n.length > 500) return { error: "A note is 1 to 500 characters." };
    body.note = n;
  }
  return { path, body };
}

// The check the setting box makes before sending; the server checks again. `def` is one entry of GET /api/admin/settings.
export function settingProblem(def, raw) {
  const v = String(raw ?? "").trim();
  if (!def) return "Choose a setting.";
  if (def.type === "bool") return ["true", "false", "on", "off"].includes(v.toLowerCase()) ? null : "Use true or false.";
  if (def.type === "choice") return (def.choices || []).includes(v.toLowerCase()) ? null : `Use one of: ${(def.choices || []).join(", ")}.`;
  const n = Number(v);
  if (v === "" || !Number.isFinite(n) || (def.type === "int" && !Number.isInteger(n))) return "Enter a number.";
  if (n < def.min || n > def.max) return `Use a number from ${def.min} to ${def.max}.`;
  return null;
}

export function settingBadge(def) {
  const parts = [def.source === "database" ? "set in the dashboard" : "from the environment"];
  parts.push(def.restart_required ? "needs a restart" : "takes effect without a restart");
  if (def.customer_visible) parts.push("customers can see it");
  return parts.join(", ");
}

export { REASON_MAX, REASON_MIN };
