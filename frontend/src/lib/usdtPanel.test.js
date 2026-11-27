import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { EXPIRED_TITLE, POLL_SECONDS, assetBox, clockOffsetMs, panelView, shouldResume } from "./usdtPanel.js";

const NOW = Date.parse("2026-11-27T12:00:00Z");
const USDT_BSC = { code: "usdtbsc", known: true, asset: "USDT", network: "the BSC network (BEP20)", network_short: "BSC (BEP20)", examples: "BNB, USDT on Tron or USDT on Ethereum", label: "USDT on BSC (BEP20)" };
const inv = (over = {}) => ({ reference: "mbacrypto_abc", price_usd: "10.00", pay_coin: USDT_BSC, pay_amount: null, issued_at: "2026-11-27T11:50:00Z", stage: "waiting", short_usd: null, detected_at: null, detected_after_expiry: false, ...over });
const status = (over = {}, top = {}) => ({ mode: "enforce", server_time: "2026-11-27T12:00:00Z", invoice: inv(over), ...top });
const src = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

test("the poll is every 10 seconds", () => {
  assert.equal(POLL_SECONDS, 10);
});

test("waiting: plain words and nothing that counts time", () => {
  const v = panelView(status());
  assert.equal(v.headline, "Waiting for your transfer");
  assert.equal("countdown" in v || "secondsLeft" in v, false);
});

test("a stage never depends on the clock: an old waiting invoice is still just waiting until the provider says otherwise", () => {
  const v = panelView(status({ issued_at: "2020-01-01T00:00:00Z" }));
  assert.equal(v.headline, "Waiting for your transfer");
  assert.equal(v.showNewInvoice, false);
});

test("expired comes only from the provider's own expired status, with a new-invoice button", () => {
  const v = panelView(status({ stage: "expired" }));
  assert.equal(v.headline, EXPIRED_TITLE);
  assert.equal(v.showNewInvoice, true);
  assert.equal(v.assetBox, null); // nothing tells the customer to pay an expired invoice
});

test("transfer seen: confirming; confirmed: done", () => {
  assert.equal(panelView(status({ stage: "confirming" })).headline, "Transfer seen, confirming");
  const done = panelView(status({ stage: "confirmed" }));
  assert.equal(done.headline, "Payment confirmed");
  assert.equal(done.terminal, true);
  assert.equal(done.showNewInvoice, false);
  assert.equal(done.assetBox, null);
});

test("underpaid says how much is short and shows the reference", () => {
  const v = panelView(status({ stage: "underpaid", short_usd: "2.50" }));
  assert.equal(v.headline, "Underpaid");
  assert.match(v.detail, /short by USD 2\.50/);
  assert.equal(v.showReference, true);
  assert.match(panelView(status({ stage: "underpaid" })).detail, /less than the price/);
});

test("a transfer seen after the PROVIDER reported the payment expired shows the reference for support; our own age never does", () => {
  const late = panelView(status({ stage: "confirming", detected_after_expiry: true }));
  assert.equal(late.headline, "A payment was detected after this invoice expired");
  assert.equal(late.showReference, true);
  assert.equal(late.reference, "mbacrypto_abc");
  assert.equal(panelView(status({ stage: "confirmed", detected_after_expiry: true })).headline, "Payment confirmed");
  assert.equal(panelView(status({ stage: "confirming", issued_at: "2020-01-01T00:00:00Z" })).headline, "Transfer seen, confirming");
});

test("a payment in another coin or network shows the exact message with the reference and nothing that could take another payment", () => {
  const v = panelView(status({ stage: "wrong_coin" }));
  assert.equal(v.headline, "Wrong coin or network");
  assert.equal(v.detail, "We received a payment in a different coin or network. It was not credited automatically. Contact support and quote reference mbacrypto_abc.");
  assert.equal(v.showNewInvoice, false);
  assert.equal(v.assetBox, null);
});

test("failed and refunded say so and keep the reference", () => {
  assert.equal(panelView(status({ stage: "failed" })).showNewInvoice, true);
  assert.equal(panelView(status({ stage: "refunded" })).showReference, true);
});

test("no invoice, no panel", () => {
  assert.equal(panelView({ invoice: null }), null);
  assert.equal(panelView(null), null);
});

// ---- the box that says what to pay
test("the box names the coin and network in one bold line, the exact amount when we have it, and the warning, word for word", () => {
  const box = assetBox(inv({ pay_amount: "9.954" }));
  assert.equal(box.heading, "Pay with USDT on the BSC network (BEP20)");
  assert.equal(box.amount, "Amount: 9.954 USDT");
  assert.equal(box.warning, "Send only USDT on the BSC (BEP20) network. Any other coin or network, for example BNB, USDT on Tron or USDT on Ethereum, is not credited automatically and may be lost.");
});

test("with no exact amount the box shows the price in USD and says where the exact amount is", () => {
  const box = assetBox(inv());
  assert.equal(box.amount, "Price: USD 10.00. The exact amount in USDT is shown on the payment page.");
});

test("the coin and network come from the setting: another configured coin gives other words, and an unknown code is shown as the bare code", () => {
  const trc = { code: "usdttrc20", known: true, asset: "USDT", network: "the Tron network (TRC20)", network_short: "Tron (TRC20)", examples: "BNB or USDT on Ethereum", label: "USDT on Tron (TRC20)" };
  const box = assetBox(inv({ pay_coin: trc, pay_amount: "10.01" }));
  assert.equal(box.heading, "Pay with USDT on the Tron network (TRC20)");
  assert.match(box.warning, /^Send only USDT on the Tron \(TRC20\) network\./);
  assert.ok(!box.warning.includes("BSC"));
  const unknown = assetBox(inv({ pay_coin: { code: "xyz", known: false, asset: "XYZ", network: null, network_short: null, examples: null, label: "XYZ" } }));
  assert.equal(unknown.heading, "Pay with XYZ");
  assert.match(unknown.warning, /^Send only XYZ, exactly as the payment page shows\./);
  assert.equal(assetBox(inv({ pay_coin: null })), null);
});

test("the box is shown while a transfer is expected or on its way and is gone once confirmed", () => {
  for (const stage of ["waiting", "confirming", "underpaid"]) assert.ok(panelView(status({ stage })).assetBox, stage);
  for (const stage of ["confirmed", "expired", "failed", "refunded", "wrong_coin"]) assert.equal(panelView(status({ stage })).assetBox, null, stage);
});

test("the panel prints the box with the warning token and is not behind an info icon", () => {
  const panel = src("../components/UsdtPanel.jsx");
  assert.ok(panel.includes("view.assetBox.heading") && panel.includes("view.assetBox.warning") && panel.includes("view.assetBox.amount"));
  assert.ok(/border-warn/.test(panel) && /bg-warn-soft/.test(panel) && /text-fg/.test(panel));
  assert.ok(/text-xl[^"]*font-bold|font-bold[^"]*text-xl/.test(panel), "the line is bold and large");
  assert.ok(panel.includes("{view.assetBox && ("), "the box is rendered whenever the view has one");
  assert.ok(!panel.includes("InfoHelp") && !panel.includes("HelpTip"));
  assert.ok(panel.indexOf("view.assetBox.heading") < panel.indexOf("Open the payment page"), "the box sits above the Open the payment page button");
});

// ---- a panel opens again after a reload only while a payment is under way
test("a panel opens by itself after a reload: unfinished invoices of the last day, a payment found after the provider's expiry, and a wrong-coin payment", () => {
  assert.equal(shouldResume(status(), NOW), true);
  assert.equal(shouldResume(status({ issued_at: "2026-11-26T10:00:00Z" }), NOW), false); // more than a day old and never paid
  assert.equal(shouldResume(status({ stage: "confirming", issued_at: "2026-11-20T10:00:00Z" }), NOW), false);
  assert.equal(shouldResume(status({ stage: "confirmed" }), NOW), false);
  assert.equal(shouldResume(status({ stage: "expired" }), NOW), false);
  assert.equal(shouldResume(status({ stage: "expired", detected_after_expiry: true }), NOW), true);
  assert.equal(shouldResume(status({ stage: "wrong_coin", issued_at: "2026-11-01T10:00:00Z" }), NOW), true);
  assert.equal(shouldResume({ invoice: null }, NOW), false);
  assert.equal(clockOffsetMs("2026-11-27T12:00:00Z", NOW + 3_600_000), -3_600_000);
  assert.equal(clockOffsetMs("garbage", NOW), 0);
});

test("the panel has no clock of its own: no countdown, no timer, no end time, no check route", () => {
  const panel = src("../components/UsdtPanel.jsx");
  const lib = src("./usdtPanel.js");
  const code = (panel + lib).replace(/^\s*\/\/.*$/gm, "").replace(/ \/\/ .*$/gm, ""); // the code without its comments
  assert.ok(!/countdown|Time left|useNow|window_ends_at|window_minutes|remainingSeconds|secondsLeft/i.test(code), "no countdown code");
  assert.ok(!/1200|20 \* 60/.test(panel));
  assert.ok(!panel.includes("/billing/crypto/check"));
  assert.equal(panel.split('api.get("/billing/crypto/status")').length - 1, 2, "the poll and the resume check read our own status route");
  assert.ok(!/api\.[a-z]+\(\s*["'](?!\/billing\/crypto\/status)/.test(panel), "only our own status route is read");
  assert.equal(panel.split("setInterval(").length - 1, 1, "the only timer is the status poll");
});

test("the USDT button opens the panel instead of leaving the page", () => {
  const buttons = src("../components/SubscribeCard.jsx");
  assert.ok(buttons.includes("UsdtPanel"));
  assert.ok(buttons.includes('c.kind === "crypto"'));
});
