import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { CHECK_COOLDOWN_SECONDS, EXPIRED_TITLE, POLL_SECONDS, checkErrorMessage, checkMessage, clockOffsetMs, countdownText, panelView, remainingSeconds, shouldResume } from "./usdtPanel.js";

const T0 = Date.parse("2026-11-27T10:00:00Z");
const inv = (over = {}) => ({ reference: "mbacrypto_abc", price_usd: "10.00", issued_at: "2026-11-27T10:00:00Z", window_ends_at: "2026-11-27T10:20:00Z", stage: "waiting", short_usd: null, detected_at: null, detected_after_expiry: false, ...over });
const status = (over = {}, top = {}) => ({ mode: "enforce", check_available: true, server_time: "2026-11-27T10:00:00Z", window_minutes: 20, invoice: inv(over), ...top });
const src = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

test("the poll is every 10 seconds and the button waits 15", () => {
  assert.equal(POLL_SECONDS, 10);
  assert.equal(CHECK_COOLDOWN_SECONDS, 15);
});

test("the countdown runs down from the invoice's own end time and stays at zero: it never starts again", () => {
  assert.equal(remainingSeconds("2026-11-27T10:20:00Z", T0), 1200);
  assert.equal(remainingSeconds("2026-11-27T10:20:00Z", T0 + 600_000), 600);
  assert.equal(remainingSeconds("2026-11-27T10:20:00Z", T0 + 1_199_500), 1);
  for (const later of [1_200_000, 1_200_001, 1_300_000, 3_600_000, 86_400_000]) assert.equal(remainingSeconds("2026-11-27T10:20:00Z", T0 + later), 0, `at +${later} ms`);
  assert.equal(remainingSeconds("not a date", T0), 0);
  assert.equal(countdownText(1200), "20:00");
  assert.equal(countdownText(61), "01:01");
  assert.equal(countdownText(0), "00:00");
  assert.equal(countdownText(-5), "00:00");
});

test("a wrong device clock does not move the countdown: the server's time is the reference", () => {
  const offset = clockOffsetMs("2026-11-27T10:00:00Z", T0 + 3_600_000); // the device is an hour fast
  assert.equal(remainingSeconds("2026-11-27T10:20:00Z", T0 + 3_600_000, offset), 1200);
  assert.equal(clockOffsetMs("garbage", T0), 0);
});

test("waiting, inside the window: plain words, the countdown and the button", () => {
  const v = panelView(status(), T0 + 60_000);
  assert.equal(v.headline, "Waiting for your transfer");
  assert.equal(v.countdown, "19:00");
  assert.equal(v.showSent, true);
  assert.equal(v.showNewInvoice, false);
});

test("at zero the panel says the invoice expired, offers a new invoice, and the countdown is gone for good", () => {
  for (const later of [1_200_000, 1_260_000, 7_200_000]) {
    const v = panelView(status(), T0 + later);
    assert.equal(v.headline, EXPIRED_TITLE);
    assert.equal(v.countdown, null);
    assert.equal(v.showNewInvoice, true);
    assert.equal(v.secondsLeft, 0);
  }
});

test("the server saying expired shows the same, even inside our own window", () => {
  const v = panelView(status({ stage: "expired" }), T0 + 60_000);
  assert.equal(v.headline, EXPIRED_TITLE);
  assert.equal(v.showNewInvoice, true);
});

test("transfer seen: confirming; confirmed: done with no buttons; neither is turned into expired by the clock", () => {
  const c = panelView(status({ stage: "confirming" }), T0 + 5_000_000);
  assert.equal(c.headline, "Transfer seen, confirming");
  assert.equal(c.showNewInvoice, false);
  const done = panelView(status({ stage: "confirmed" }), T0 + 5_000_000);
  assert.equal(done.headline, "Payment confirmed");
  assert.equal(done.terminal, true);
  assert.equal(done.showSent, false);
  assert.equal(done.showNewInvoice, false);
});

test("underpaid says how much is short and shows the reference", () => {
  const v = panelView(status({ stage: "underpaid", short_usd: "2.50" }), T0 + 1000);
  assert.equal(v.headline, "Underpaid");
  assert.match(v.detail, /short by USD 2\.50/);
  assert.equal(v.showReference, true);
  assert.equal(v.reference, "mbacrypto_abc");
  assert.match(panelView(status({ stage: "underpaid", short_usd: null }), T0).detail, /less than the price/);
});

test("a payment detected after the invoice expired shows a message with the reference for support, not a new countdown", () => {
  const v = panelView(status({ stage: "confirming", detected_after_expiry: true, detected_at: "2026-11-27T10:30:00Z" }), T0 + 2_000_000);
  assert.equal(v.headline, "A payment was detected after this invoice expired");
  assert.equal(v.showReference, true);
  assert.equal(v.reference, "mbacrypto_abc");
  assert.equal(v.countdown, null);
  assert.equal(panelView(status({ stage: "confirmed", detected_after_expiry: true }), T0).headline, "Payment confirmed"); // a confirmed payment is simply confirmed
});

test("the check button shows only when the server can check (not in off mode)", () => {
  assert.equal(panelView(status({}, { check_available: false }), T0).showSent, false);
  assert.equal(panelView(status({}, { check_available: true }), T0).showSent, true);
});

test("no invoice, no panel", () => {
  assert.equal(panelView({ invoice: null }, T0), null);
  assert.equal(panelView(null, T0), null);
});

test("a panel opens by itself after a reload only while a payment is under way", () => {
  assert.equal(shouldResume(status(), T0 + 1000), true);
  assert.equal(shouldResume(status(), T0 + 1_300_000), false); // an expired waiting invoice: nothing to resume
  assert.equal(shouldResume(status({ stage: "confirming" }), T0 + 9_000_000), true);
  assert.equal(shouldResume(status({ stage: "underpaid" }), T0 + 9_000_000), true);
  assert.equal(shouldResume(status({ stage: "confirmed" }), T0), false);
  assert.equal(shouldResume(status({ stage: "expired" }), T0), false);
  assert.equal(shouldResume(status({ stage: "expired", detected_after_expiry: true }), T0), true);
  assert.equal(shouldResume({ invoice: null }, T0), false);
});

test("the answers to the check button are shown in plain words", () => {
  assert.equal(checkMessage({ stage: "underpaid", short_usd: "4.00", text: "x" }), "We received less than the price. You are short by USD 4.00.");
  assert.equal(checkMessage({ stage: "waiting", text: "Waiting for your transfer." }), "Waiting for your transfer.");
  assert.equal(checkMessage(null), "");
  assert.match(checkErrorMessage(429), /wait a few seconds/);
  assert.match(checkErrorMessage(404), /not available/);
  assert.match(checkErrorMessage(503, "We could not ask the payment provider just now."), /provider/);
});

test("the panel never computes its own end time: nothing in the panel code or the pay buttons can start the countdown again", () => {
  const panel = src("../components/UsdtPanel.jsx");
  assert.ok(!/1200|20 \* 60|window_minutes/.test(panel), "the panel must not build an end time of its own");
  assert.ok(panel.includes("panelView(") && panel.includes("window_ends_at") === false);
  assert.ok(panel.includes("POLL_SECONDS") && panel.includes("/billing/crypto/status"));
  assert.ok(panel.includes("/billing/crypto/check") && panel.includes("SENT_LABEL") && panel.includes("NEW_INVOICE_LABEL"));
  assert.ok(!/api\.[a-z]+\(\s*["'](?!\/billing\/crypto\/(status|check|initialize))/.test(panel), "no other route is called");
  assert.equal(panel.split('api.get("/billing/crypto/status")').length - 1, 2, "the poll and the resume check read our own status route");
  assert.ok(!panel.includes('api.get("/billing/crypto/check")') && panel.split('api.post("/billing/crypto/check"').length - 1 === 1, "the check is one POST");
  assert.equal(panel.split("setCooldownUntil(Date.now() + CHECK_COOLDOWN_SECONDS * 1000)").length - 1, 2, "the button waits after a check and after a 429");
});

test("the USDT button opens the panel instead of leaving the page, and only the existing checkout route is called", () => {
  const buttons = src("../components/SubscribeCard.jsx");
  assert.ok(buttons.includes("UsdtPanel"));
  assert.ok(buttons.includes('c.kind === "crypto"'));
});
