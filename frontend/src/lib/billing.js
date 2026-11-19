// Billing Stage 4 helpers: what the Activate screen and the subscription card show, from what GET /api/billing/options sends (the server settings decide prices and what is available).
// Pure functions only. Nothing here holds a price, a key, a reference or a link. Run with: node --test frontend/src/lib/billing.test.js (also run by tests/test_frontend_lib.py)

// Paystack adds its fee to the plan price at checkout (observed in test mode: price plus fee).
export const FEE_NOTE = "Paystack adds its own fee at checkout, so the amount you are charged is a little more than the price shown.";
// Word for word from the Terms of Service, section 11 (frontend/src/legalContent.js; a test fails if the two drift apart).
export const CANCEL_WORDING = "Where access is sold as a subscription, cancelling stops the next charge, and your access continues until the end of the period you have already paid for.";

export const CHECKOUT_PATH = { card: "/billing/subscription/initialize", crypto: "/billing/crypto/initialize" };

function groupThousands(whole) {
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// An amount in the smallest currency unit, written with a currency code and two decimals. Not a number -> null (the screen then shows no price rather than a wrong one).
export function formatMinor(amountMinor, currency) {
  const n = Number(amountMinor);
  if (!Number.isFinite(n) || n < 0 || !currency) return null;
  const cents = Math.round(n);
  const whole = Math.floor(cents / 100);
  return `${currency} ${groupThousands(String(whole))}.${String(cents % 100).padStart(2, "0")}`;
}

// A decimal dollar string, written with the USD code and two decimals.
export function formatUsd(priceUsd) {
  const n = Number(priceUsd);
  if (!Number.isFinite(n) || n <= 0) return null;
  const cents = Math.round(n * 100);
  return `USD ${groupThousands(String(Math.floor(cents / 100)))}.${String(cents % 100).padStart(2, "0")}`;
}

export function dateText(iso) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return null;
  return `${d.getUTCDate()} ${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

// The ways this customer may pay right now: only channels the server says are available AND may start. Card first.
export function channels(options) {
  const out = [];
  if (options?.card?.available && options.card.can_start) {
    out.push({ kind: "card", label: "Pay by card", price: formatMinor(options.card.amount_minor, options.card.currency), note: FEE_NOTE, path: CHECKOUT_PATH.card });
  }
  if (options?.crypto?.available && options.crypto.can_start) {
    out.push({ kind: "crypto", label: "Pay with USDT", price: formatUsd(options.crypto.price_usd), note: null, path: CHECKOUT_PATH.crypto });
  }
  return out;
}

// The state card (active, grace, cancelled) or null. `none` and `legacy_lifetime` have no card; `expired` is shown inside the offer (see expiredNote).
export function stateView(options) {
  if (!options) return null;
  const end = dateText(options.period_end);
  const grace = dateText(options.grace_end);
  const days = Number(options.grace_days) || 0;
  switch (options.state) {
    case "active":
      return { kind: "active", title: "Subscription active", body: end ? (options.provider === "paystack" ? `Your card renews on ${end}.` : `Your paid period ends on ${end}. Renew before then to avoid a break.`) : "Your subscription is active." };
    case "grace":
      return { kind: "grace", title: "Payment overdue", body: `Your access continues for the ${days}-day grace period${grace ? `, until ${grace}` : ""}. Pay now to keep it.` };
    case "cancelled":
      return { kind: "cancelled", title: "Subscription cancelled", body: end ? `Your subscription will not renew. Your access continues until ${end}.` : "Your subscription will not renew." };
    default:
      return null;
  }
}

export const EXPIRED_NOTE = "Your subscription has ended. Subscribe again to keep using live trading.";

// What the Activate screen does with the options: "legacy" (nothing, the customer already has lifetime access), "offer" (a new or expired customer), "status" (a subscriber), or "off" (no options: the old screen).
export function screenKind(options) {
  if (!options) return "off";
  if (options.state === "legacy_lifetime") return "legacy";
  if (options.state === "none" || options.state === "expired") return "offer";
  return "status";
}
