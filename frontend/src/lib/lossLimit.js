// The daily loss limit field and the Bot Settings tabs: the rules the screen applies before it saves. The server checks the same things again (src/mba_bot/daily_limit.py).
// Run with: node --test frontend/src/lib/lossLimit.test.js (also run by tests/test_frontend_lib.py)
import { usd } from "./format.js";
import { TOTAL_STOP_PCT } from "./riskBands.js";

export const SUGGESTED_PCT = 5; // mirrors daily_limit.SUGGESTED_PCT; tests/test_settings_tabs.py fails if they drift

export const TABS = [
  { id: "trading", label: "Trading" },
  { id: "risk", label: "Risk" },
];

// A plain problem text, or null. `maxUsd` is the dollar level of the automatic total stop (the server sends it); the amount must be below it. Empty is a problem only when
// a limit is required (an account without one cannot go live).
export function limitProblem(raw, maxUsd, { required = false } = {}) {
  if (raw === "" || raw === null || raw === undefined) return required ? "Enter your daily loss limit in US dollars. It is required before live trading." : null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return "Your daily loss limit must be an amount above 0.";
  if (Number.isFinite(maxUsd) && maxUsd > 0 && n >= maxUsd - 1e-9) {
    return `Your daily loss limit must be below ${usd(maxUsd)}, the level where the automatic total stop acts (${TOTAL_STOP_PCT}% of your account value).`;
  }
  return null;
}

// The amount as a percent of the account value, one decimal, or null when either number is missing.
export function pctOfValue(amount, value) {
  const a = Number(amount);
  const v = Number(value);
  if (amount === "" || amount === null || amount === undefined || value === null || value === undefined) return null;
  if (!Number.isFinite(a) || !Number.isFinite(v) || v <= 0) return null;
  return Math.round((a / v) * 1000) / 10;
}

// One trade can lose more than the limit: risk % x trading capital > amount.
export function oneTradeCanExceed(riskPct, capital, amount) {
  const r = Number(riskPct);
  const c = Number(capital);
  const a = Number(amount);
  if (![r, c, a].every(Number.isFinite) || c <= 0 || a <= 0) return false;
  return (r / 100) * c > a + 1e-9;
}

export function suggestedText(value) {
  const v = Number(value);
  if (value === null || value === undefined || !Number.isFinite(v) || v <= 0) return null;
  return `${usd(Math.round(v * SUGGESTED_PCT) / 100)}`;
}

// "Your new limit of $90.00 starts at midnight Lagos time." while a raise is waiting; null otherwise.
export function pendingNotice(account) {
  const amount = account?.daily_loss_limit_pending_usd;
  if (amount === null || amount === undefined) return null;
  return `Your new limit of ${usd(amount)} starts at midnight Lagos time.`;
}

// Which tab a message from the server belongs to, so the error is shown where the field is.
export function tabForMessage(message) {
  return /capital|pair|market|selection/i.test(String(message || "")) ? "trading" : "risk";
}

// Arrow keys move between tabs and wrap; Home and End jump to the ends. Returns the new index, or null for any other key.
export function nextTabIndex(key, index, count) {
  if (key === "ArrowRight" || key === "ArrowDown") return (index + 1) % count;
  if (key === "ArrowLeft" || key === "ArrowUp") return (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

// The dashboard notice about the daily loss limit, or null. A starting limit the service set stays visible until the customer saves their own amount; a live-authorised account with no
// amount is told it needs one (it takes no new trade until it has one).
export function dashboardNotice(account) {
  if (!account) return null;
  if (account.daily_loss_limit_is_default && account.daily_loss_limit_usd != null) {
    return { kind: "default", text: `Your daily loss limit is ${usd(account.daily_loss_limit_usd)}. We set this starting amount for you. Check it on Bot Settings and save your own amount.` };
  }
  if (account.daily_loss_limit_required && account.live_trading_enabled_at) {
    return { kind: "required", text: "Set your daily loss limit on Bot Settings. The bot opens no new trades until you do." };
  }
  return null;
}
