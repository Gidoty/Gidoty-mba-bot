import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { CANCEL_WORDING, CHECKOUT_PATH, CARD_FEE_NOTICE, EXPIRED_NOTE, channels, clickAction, dateText, formatMinor, formatUsd, renewView, screenKind, stateView } from "./billing.js";

const options = (over = {}) => ({
  state: "none", provider: null, period_end: null, grace_end: null, grace_days: 5,
  card: { available: true, can_start: true, amount_minor: 1500000, currency: "NGN" },
  crypto: { available: true, can_start: true, price_usd: "10.00" },
  ...over,
});

test("prices are formatted from the numbers the server sends", () => {
  assert.equal(formatMinor(1500000, "NGN"), "NGN 15,000.00");
  assert.equal(formatMinor(1234567, "NGN"), "NGN 12,345.67");
  assert.equal(formatMinor(5, "NGN"), "NGN 0.05");
  assert.equal(formatMinor(100000000, "NGN"), "NGN 1,000,000.00");
  assert.equal(formatMinor("x", "NGN"), null);
  assert.equal(formatMinor(-1, "NGN"), null);
  assert.equal(formatMinor(100, ""), null);
  assert.equal(formatUsd("10.00"), "USD 10.00");
  assert.equal(formatUsd("7.5"), "USD 7.50");
  assert.equal(formatUsd("0"), null);
  assert.equal(formatUsd(undefined), null);
});

test("no price is written in the screen helpers: a different server price gives a different text", () => {
  const a = channels(options());
  const b = channels(options({ card: { available: true, can_start: true, amount_minor: 2000000, currency: "NGN" }, crypto: { available: true, can_start: true, price_usd: "12.00" } }));
  assert.equal(a[0].price, "NGN 15,000.00");
  assert.equal(b[0].price, "NGN 20,000.00");
  assert.equal(b[1].price, "USD 12.00");
  const source = readFileSync(new URL("./billing.js", import.meta.url), "utf8");
  assert.ok(!/15,?000|135,?000|1500000|\b10\.00\b/.test(source), "billing.js must not contain a price");
});

test("only channels the server says are available and may start are offered, card first", () => {
  assert.deepEqual(channels(options()).map((c) => c.kind), ["card", "crypto"]);
  assert.deepEqual(channels(options({ crypto: { available: false, can_start: false, price_usd: "10.00" } })).map((c) => c.kind), ["card"]);
  assert.deepEqual(channels(options({ card: { available: false, can_start: false, amount_minor: 1, currency: "NGN" } })).map((c) => c.kind), ["crypto"]);
  assert.deepEqual(channels(options({ crypto: { available: true, can_start: false, price_usd: "10.00" } })).map((c) => c.kind), ["card"]);
  assert.deepEqual(channels(options({ card: { available: true, can_start: false, amount_minor: 1, currency: "NGN" }, crypto: { available: false, can_start: false, price_usd: "1" } })), []);
  assert.deepEqual(channels(null), []);
  assert.deepEqual(channels({}), []);
});

test("the card channel carries no note on the Activate screen and calls the subscription route, crypto calls the crypto route", () => {
  const [card, crypto] = channels(options());
  assert.equal(card.note, null); // no fee text on the Activate screen
  assert.equal(crypto.note, null);
  assert.equal(card.path, "/billing/subscription/initialize");
  assert.equal(crypto.path, "/billing/crypto/initialize");
  assert.equal(crypto.note, null);
  assert.deepEqual(CHECKOUT_PATH, { card: "/billing/subscription/initialize", crypto: "/billing/crypto/initialize" });
  assert.ok(!Object.values(CHECKOUT_PATH).includes("/billing/initialize")); // the lifetime route is never called by the new screen
});

test("the cancel wording is the Terms of Service wording, word for word", () => {
  const terms = readFileSync(new URL("../legalContent.js", import.meta.url), "utf8");
  assert.ok(terms.includes(CANCEL_WORDING), "the sentence must appear in section 11 of the Terms");
});

test("subscription states: active, grace, cancelled, with dates; none and lifetime have no card", () => {
  const end = "2026-12-05T10:00:00Z";
  assert.deepEqual(stateView(options({ state: "active", provider: "paystack", period_end: end })), { kind: "active", title: "Subscription active", body: "Your card renews on 5 Dec 2026." });
  assert.match(stateView(options({ state: "active", provider: "nowpayments", period_end: end })).body, /paid period ends on 5 Dec 2026. Renew before then/);
  const g = stateView(options({ state: "grace", period_end: end, grace_end: "2026-12-10T10:00:00Z", grace_days: 5 }));
  assert.equal(g.kind, "grace");
  assert.equal(g.title, "Renewal overdue");
  assert.match(g.body, /Your paid period ended on 5 Dec 2026\. Your access continues for the 5-day grace period, until 10 Dec 2026\. Renew now to keep it\./);
  assert.match(stateView(options({ state: "cancelled", period_end: end })).body, /will not renew. Your access continues until 5 Dec 2026/);
  for (const state of ["none", "expired", "legacy_lifetime", "weird"]) assert.equal(stateView(options({ state })), null);
  assert.equal(stateView(null), null);
  assert.match(EXPIRED_NOTE, /has ended\. Renew/);
  assert.equal(dateText("not a date"), null);
});

test("which screen: the old one when there are no options, nothing for lifetime customers, the offer for new customers, Renew for a lapsed one", () => {
  assert.equal(screenKind(null), "off");
  assert.equal(screenKind(undefined), "off");
  assert.equal(screenKind(options({ state: "legacy_lifetime" })), "legacy");
  assert.equal(screenKind(options({ state: "none" })), "offer");
  assert.equal(screenKind(options({ state: "expired" })), "renew");
  for (const s of ["active", "grace", "cancelled"]) assert.equal(screenKind(options({ state: s })), "status");
});

test("a lapsed customer sees Renew wording and the lapse date, not the first-time offer; the buttons say Renew", () => {
  const lapsed = options({ state: "expired", period_end: "2026-11-10T10:00:00Z", grace_end: "2026-11-15T10:00:00Z", grace_days: 5 });
  assert.equal(screenKind(lapsed), "renew");
  assert.deepEqual(renewView(lapsed), { title: "Renew your subscription", body: "Your subscription lapsed on 15 Nov 2026. Renew to keep using live trading." });
  assert.equal(renewView(options({ state: "expired", period_end: "2026-11-10T10:00:00Z", grace_end: null })).body, "Your subscription lapsed on 10 Nov 2026. Renew to keep using live trading.");
  assert.equal(renewView(options({ state: "expired" })).body, EXPIRED_NOTE);
  assert.deepEqual(channels(lapsed).map((c) => c.label), ["Renew by card", "Renew with USDT"]);
  assert.deepEqual(channels(options({ state: "grace", period_end: "2026-11-10T10:00:00Z" })).map((c) => c.label), ["Renew by card", "Renew with USDT"]);
  assert.deepEqual(channels(options({ state: "none" })).map((c) => c.label), ["Pay by card", "Pay with USDT"]); // a first-time customer keeps the first-time wording
  for (const state of ["none", "active", "grace", "cancelled", "legacy_lifetime"]) assert.equal(renewView(options({ state })), null);
});

test("the fee notice appears only after the click, for the card only, and only Continue calls the checkout route", () => {
  const [card, crypto] = channels(options());
  assert.equal(clickAction(card), "confirm"); // a card click shows the notice and waits: no request, no redirect
  assert.equal(clickAction(crypto), "go"); // USDT is unchanged
  assert.equal(CARD_FEE_NOTICE, "Paystack may add a small processing fee at checkout, so the total on the next page can be a little higher than the price shown.");
  const src = readFileSync(new URL("../components/SubscribeCard.jsx", import.meta.url), "utf8");
  assert.ok(src.includes("Continue to Paystack") && src.includes("{CARD_FEE_NOTICE}"));
  assert.ok(src.includes('className="mb-2 text-xs text-muted"')); // small, muted, normal weight, like the cancel wording
  assert.equal(src.split("api.post(").length - 1, 1); // the checkout route is called from one place only: go()
  assert.ok(src.includes("onClick={() => click(c)}") && src.includes("onClick={() => go(c)}")); // the pay button only clicks; Continue calls go
  assert.ok(!src.includes("font-semibold text-muted") && !/CARD_FEE_NOTICE[^\n]*font-/.test(src));
  const activate = readFileSync(new URL("./billing.js", import.meta.url), "utf8");
  assert.ok(!/note: CARD_FEE_NOTICE/.test(activate) && !activate.includes("FEE_NOTE")); // the notice is not part of the channel the Activate screen draws
  for (const label of ["Renew by card", "Renew with USDT"]) assert.ok(activate.includes(label)); // #81's Renew labels stay
});

test("Terms section 11 carries the one Paystack fee sentence", () => {
  const terms = readFileSync(new URL("../legalContent.js", import.meta.url), "utf8");
  assert.ok(terms.includes("Card payments are processed by Paystack, which may add its own processing fee at checkout; the total is shown on Paystack's payment page before you pay."));
  assert.ok(terms.includes('export const TERMS_VERSION = "2026-11-23"') && terms.includes('export const PRIVACY_VERSION = "2026-11-21"') && terms.includes('export const RISK_DISCLOSURE_VERSION = "2026-11-21"'));
});
