// What the USDT payment panel shows (components/UsdtPanel.jsx), from what GET /api/billing/crypto/status sends (our own database) and what POST /api/billing/crypto/check answers. Pure functions: the countdown is
// computed from the invoice's own fixed end time, so it can never start again by itself. Nothing here grants access or decides a payment.
// Run with: node --test frontend/src/lib/usdtPanel.test.js (also run by tests/test_frontend_lib.py)

export const POLL_SECONDS = 10; // the panel reads our database this often while it is open (no call to the payment provider)
export const EXPIRED_TITLE = "This invoice expired";
export const NEW_INVOICE_LABEL = "Create a new invoice";

const ACTIVE_STAGES = ["waiting", "confirming", "underpaid", "wrong_coin"];
export const OTHER_COINS_NOTE = "Other coins or networks are not credited automatically.";

// What to send, in words: the exact coin and network the invoice asked for, the exact amount once the payment exists (it is read from the provider's record by the server), and the warning about other coins.
export function instructionText(inv) {
  if (!inv?.pay_label) return null;
  const how = inv.pay_amount ? `Send exactly ${inv.pay_amount} ${inv.pay_label}.` : `Send ${inv.pay_label}${inv.price_usd ? ` worth USD ${inv.price_usd}` : ""}. The exact amount is shown on the payment page.`;
  return `${how} ${OTHER_COINS_NOTE}`;
}

// The difference between the server's clock and this device's clock, so a wrong device clock does not move the countdown.
export function clockOffsetMs(serverTimeIso, clientNowMs) {
  const t = Date.parse(serverTimeIso);
  return Number.isFinite(t) ? t - clientNowMs : 0;
}

// Whole seconds left until the invoice's fixed end time. Never negative, and it stays 0 once the time has passed: there is no restart.
export function remainingSeconds(windowEndsAtIso, nowMs, offsetMs = 0) {
  const end = Date.parse(windowEndsAtIso);
  if (!Number.isFinite(end)) return 0;
  return Math.max(0, Math.ceil((end - (nowMs + offsetMs)) / 1000));
}

export function countdownText(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

// The panel as it should look now, or null when there is no invoice. `status` is the body of GET /api/billing/crypto/status.
export function panelView(status, nowMs, offsetMs = 0) {
  const inv = status?.invoice;
  if (!inv) return null;
  const left = remainingSeconds(inv.window_ends_at, nowMs, offsetMs);
  const over = left <= 0;
  const reference = inv.reference;
  const base = { instructions: null, stage: inv.stage, reference, secondsLeft: left, countdown: null, headline: "", detail: null, showNewInvoice: false, showReference: false, terminal: false };
  if (inv.stage === "confirmed") return { ...base, headline: "Payment confirmed", detail: "Thank you. Your payment is confirmed. Refresh this page to see your subscription.", terminal: true };
  if (inv.stage === "wrong_coin") {
    return { ...base, headline: "Wrong coin or network", detail: `We received a payment in a different coin or network. It was not credited automatically. Contact support and quote reference ${reference}.`, terminal: false };
  }
  const late = Boolean(inv.detected_after_expiry);
  if (late) {
    return { ...base, headline: "A payment was detected after this invoice expired", detail: "Please send this reference to support so they can match it to your payment.", showReference: true, showNewInvoice: false };
  }
  switch (inv.stage) {
    case "confirming":
      return { ...base, headline: "Transfer seen, confirming", detail: "We can see your transfer. Confirmation can take a few minutes. You do not need to do anything else.", countdown: over ? null : countdownText(left) };
    case "underpaid":
      return { ...base, headline: "Underpaid", detail: inv.short_usd ? `We received less than the price. You are short by USD ${inv.short_usd}. Please contact support with the reference below.` : "We received less than the price. Please contact support with the reference below.", showReference: true, showNewInvoice: true };
    case "failed":
      return { ...base, headline: "The payment failed", detail: "Nothing was taken from you by us. You can create a new invoice.", showReference: true, showNewInvoice: true };
    case "refunded":
      return { ...base, headline: "The payment was refunded", detail: "Please contact support with the reference below.", showReference: true, showNewInvoice: true };
    case "expired":
      return { ...base, headline: EXPIRED_TITLE, detail: "Do not send a payment to it. Create a new invoice.", showNewInvoice: true };
    default: // waiting
      if (over) return { ...base, headline: EXPIRED_TITLE, detail: "Do not send a payment to it. Create a new invoice.", showNewInvoice: true };
      return { ...base, headline: "Waiting for your transfer", detail: "Send the payment from your wallet.", instructions: instructionText(inv), countdown: countdownText(left) };
  }
}

// Whether a panel should open by itself when the page loads (the customer reloaded while paying): an invoice that is still being paid, or one a payment was found for after it expired.
export function shouldResume(status, nowMs, offsetMs = 0) {
  const inv = status?.invoice;
  if (!inv) return false;
  if (inv.detected_after_expiry && inv.stage !== "confirmed") return true;
  if (!ACTIVE_STAGES.includes(inv.stage)) return false;
  if (inv.stage === "waiting") return remainingSeconds(inv.window_ends_at, nowMs, offsetMs) > 0;
  return true;
}

