// What the USDT payment panel shows (components/UsdtPanel.jsx), from what GET /api/billing/crypto/status sends (our own database). Pure functions. The stages come ONLY from what the payment provider reported
// (waiting, confirming, confirmed, underpaid, expired, failed, refunded, wrong coin or network): the panel keeps no clock of its own and has no countdown (the provider's own payment page shows one). The one use of the
// clock is deciding whether a panel for an old, unfinished invoice should open again by itself after a reload.
// Run with: node --test frontend/src/lib/usdtPanel.test.js (also run by tests/test_frontend_lib.py)

export const POLL_SECONDS = 10; // the panel reads our database this often while it is open (no call to the payment provider)
export const EXPIRED_TITLE = "This invoice expired";
export const NEW_INVOICE_LABEL = "Create a new invoice";
export const RESUME_HOURS = 24;

const ACTIVE_STAGES = ["waiting", "confirming", "underpaid"];
const BOX_STAGES = ["waiting", "confirming", "underpaid"]; // while a transfer is expected or on its way; gone once confirmed (and not shown for an expired, failed, refunded or wrong-coin invoice, where the page says what to do instead)

// The difference between the server's clock and this device's clock (used only for the reload rule below).
export function clockOffsetMs(serverTimeIso, clientNowMs) {
  const t = Date.parse(serverTimeIso);
  return Number.isFinite(t) ? t - clientNowMs : 0;
}

// The box that says what to pay, in words that come from the invoice's own coin (NOWPAYMENTS_PAY_CURRENCY, through the status route): the coin and network in one bold line, the exact amount when our own data has it (otherwise
// the price in USD), and the warning about other coins. `pay_coin.known` false means the server has no friendly name for the configured code, so the bare code is shown.
export function assetBox(inv) {
  const c = inv?.pay_coin;
  if (!c || !c.asset) return null;
  const heading = c.known ? `Pay with ${c.asset} on ${c.network}` : `Pay with ${c.asset}`;
  const amount = inv.pay_amount ? `Amount: ${inv.pay_amount} ${c.asset}` : inv.price_usd ? `Price: USD ${inv.price_usd}. The exact amount in ${c.asset} is shown on the payment page.` : null;
  const warning = c.known
    ? `Send only ${c.asset} on the ${c.network_short} network. Any other coin or network, for example ${c.examples}, is not credited automatically and may be lost.`
    : `Send only ${c.asset}, exactly as the payment page shows. Any other coin or network is not credited automatically and may be lost.`;
  return { heading, amount, warning };
}

// The panel as it should look now, or null when there is no invoice. `status` is the body of GET /api/billing/crypto/status.
export function panelView(status) {
  const inv = status?.invoice;
  if (!inv) return null;
  const reference = inv.reference;
  const base = { stage: inv.stage, reference, assetBox: BOX_STAGES.includes(inv.stage) ? assetBox(inv) : null, headline: "", detail: null, showNewInvoice: false, showReference: false, terminal: false };
  if (inv.stage === "confirmed") return { ...base, headline: "Payment confirmed", detail: "Thank you. Your payment is confirmed. Refresh this page to see your subscription.", terminal: true };
  if (inv.stage === "wrong_coin") {
    return { ...base, headline: "Wrong coin or network", detail: `We received a payment in a different coin or network. It was not credited automatically. Contact support and quote reference ${reference}.` };
  }
  if (inv.detected_after_expiry) {
    // the provider said this payment expired, and a transfer was seen after that
    return { ...base, assetBox: null, headline: "A payment was detected after this invoice expired", detail: "Please send this reference to support so they can match it to your payment.", showReference: true };
  }
  switch (inv.stage) {
    case "confirming":
      return { ...base, headline: "Transfer seen, confirming", detail: "We can see your transfer. Confirmation can take a few minutes. You do not need to do anything else." };
    case "underpaid":
      return { ...base, headline: "Underpaid", detail: inv.short_usd ? `We received less than the price. You are short by USD ${inv.short_usd}. Please contact support with the reference below.` : "We received less than the price. Please contact support with the reference below.", showReference: true, showNewInvoice: true };
    case "failed":
      return { ...base, headline: "The payment failed", detail: "Nothing was taken from you by us. You can create a new invoice.", showReference: true, showNewInvoice: true };
    case "refunded":
      return { ...base, headline: "The payment was refunded", detail: "Please contact support with the reference below.", showReference: true, showNewInvoice: true };
    case "expired":
      return { ...base, headline: EXPIRED_TITLE, detail: "Do not send a payment to it. Create a new invoice.", showNewInvoice: true };
    default: // waiting
      return { ...base, headline: "Waiting for your transfer", detail: "Send the payment from your wallet." };
  }
}

// Whether a panel opens by itself when the page loads (the customer reloaded while paying): an invoice still being paid and made in the last RESUME_HOURS, a payment found after the provider's expiry, or a wrong-coin
// payment (that one stays until support has dealt with it).
export function shouldResume(status, nowMs, offsetMs = 0) {
  const inv = status?.invoice;
  if (!inv) return false;
  if (inv.stage === "wrong_coin" || (inv.detected_after_expiry && inv.stage !== "confirmed")) return true;
  if (!ACTIVE_STAGES.includes(inv.stage)) return false;
  const issued = Date.parse(inv.issued_at);
  return Number.isFinite(issued) && nowMs + offsetMs - issued <= RESUME_HOURS * 3_600_000;
}
