// What the Subscriptions page and the Dashboard's one status line show. Pure functions on what GET /api/billing/options and /api/auth/me already send: nothing here decides who may pay or what access is.
// Run with: node --test frontend/src/lib/subscriptions.test.js (also run by tests/test_frontend_lib.py)
import { channels, dateText, renewView, screenKind, stateView } from "./billing.js";

export const SUBSCRIPTIONS_ROUTE = "subscriptions";
export const DASHBOARD_LINK_TEXT = "Open Subscriptions";

// Billing off (the old one-time activation): the sentence the Dashboard used to show above its Activate button.
export const ACTIVATE_BODY = "Activating your account unlocks the full trading environment. You'll still choose, separately and explicitly, if and when to connect real funds - activating does not do that by itself.";

// The renewal or expiry date as one line, or null when the server did not send the date.
export function dateLine(options) {
  if (!options) return null;
  const end = dateText(options.period_end);
  const grace = dateText(options.grace_end);
  switch (options.state) {
    case "active":
      return end ? (options.provider === "paystack" ? `Renews on ${end}.` : `Paid period ends on ${end}.`) : null;
    case "grace":
      return end ? `Paid period ended on ${end}${grace ? `; grace period until ${grace}` : ""}.` : null;
    case "cancelled":
      return end ? `Access ends on ${end}.` : null;
    case "expired":
      return dateText(options.grace_end) || end ? `Lapsed on ${grace || end}.` : null;
    default:
      return null;
  }
}

// The Subscriptions page. `billing` is what useBillingOptions returns ({status: loading|ready|off, options}); `customer` is /api/auth/me.
// pay: "offer" (a new or lapsed customer: the offer card with the pay buttons), "renew" (a subscriber who may pay now: the pay buttons), "legacy" (billing off, not activated: the one Activate button), "none".
export function subscriptionsView(billing, customer) {
  if (!billing || billing.status === "loading") return { kind: "loading", title: "Subscriptions", body: null, dateLine: null, pay: "none" };
  if (billing.status === "off") {
    return customer?.has_paid
      ? { kind: "activated", title: "Account activated", body: "Your account is activated. No payment is due.", dateLine: null, pay: "none" }
      : { kind: "activate", title: "Activate your account", body: ACTIVATE_BODY, dateLine: null, pay: "legacy" };
  }
  const options = billing.options;
  const kind = screenKind(options);
  if (kind === "legacy") return { kind, title: "Lifetime access", body: "Your account has lifetime access. No payment is due.", dateLine: null, pay: "none" };
  if (kind === "offer") return { kind, title: "No active subscription", body: "You are using practice mode. A monthly subscription unlocks the full trading environment.", dateLine: null, pay: "offer" };
  if (kind === "renew") {
    const r = renewView(options);
    return { kind, title: r.title, body: r.body, dateLine: dateLine(options), pay: "offer" };
  }
  const v = stateView(options);
  return { kind, title: v ? v.title : "Subscription", body: v ? v.body : null, dateLine: dateLine(options), pay: channels(options).length > 0 ? "renew" : "none" };
}

// The Dashboard's one line when the account is not activated, or null. The pay buttons are on the Subscriptions page, not on the Dashboard.
export function dashboardLine(billing) {
  if (billing && billing.status === "ready" && screenKind(billing.options) === "renew") return "Your subscription has ended. Renew it in Subscriptions.";
  return "Your account is not activated yet. The payment options are in Subscriptions.";
}
