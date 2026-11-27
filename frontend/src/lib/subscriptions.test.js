import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { NAV_ITEMS } from "../constants.js";
import { dashboardLine, dateLine, subscriptionsView } from "./subscriptions.js";

const options = (over = {}) => ({
  state: "none", provider: null, period_end: null, grace_end: null, grace_days: 5,
  card: { available: true, can_start: true, amount_minor: 1500000, currency: "NGN" },
  crypto: { available: true, can_start: true, price_usd: "10.00" },
  ...over,
});
const ready = (o) => ({ status: "ready", options: o });
const src = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

test("a new customer sees the offer and the pay buttons are on the page", () => {
  const v = subscriptionsView(ready(options()), { has_paid: false });
  assert.equal(v.kind, "offer");
  assert.equal(v.pay, "offer");
  assert.equal(v.title, "No active subscription");
});

test("a subscriber sees the state, the renewal or expiry date, and pay buttons only when a channel may start", () => {
  const active = subscriptionsView(ready(options({ state: "active", provider: "paystack", period_end: "2026-12-05T00:00:00Z", card: { available: true, can_start: false, amount_minor: 1, currency: "NGN" }, crypto: { available: true, can_start: false, price_usd: "10.00" } })), { has_paid: true });
  assert.equal(active.title, "Subscription active");
  assert.equal(active.dateLine, "Renews on 5 Dec 2026.");
  assert.equal(active.pay, "none");
  const early = subscriptionsView(ready(options({ state: "active", provider: "nowpayments", period_end: "2026-12-05T00:00:00Z", card: { available: false, can_start: false, amount_minor: 1, currency: "NGN" } })), { has_paid: true });
  assert.equal(early.dateLine, "Paid period ends on 5 Dec 2026.");
  assert.equal(early.pay, "renew"); // the renewal window is open for USDT
  const grace = subscriptionsView(ready(options({ state: "grace", period_end: "2026-11-01T00:00:00Z", grace_end: "2026-11-06T00:00:00Z" })), { has_paid: true });
  assert.equal(grace.dateLine, "Paid period ended on 1 Nov 2026; grace period until 6 Nov 2026.");
  assert.equal(grace.pay, "renew");
  const cancelled = subscriptionsView(ready(options({ state: "cancelled", period_end: "2026-12-05T00:00:00Z", card: { available: true, can_start: false, amount_minor: 1, currency: "NGN" }, crypto: { available: true, can_start: false, price_usd: "1" } })), { has_paid: true });
  assert.equal(cancelled.dateLine, "Access ends on 5 Dec 2026.");
  assert.equal(cancelled.pay, "none");
});

test("a lapsed customer sees the Renew wording with the lapse date and the offer", () => {
  const v = subscriptionsView(ready(options({ state: "expired", period_end: "2026-10-01T00:00:00Z", grace_end: "2026-10-06T00:00:00Z" })), { has_paid: false });
  assert.equal(v.kind, "renew");
  assert.equal(v.title, "Renew your subscription");
  assert.equal(v.dateLine, "Lapsed on 6 Oct 2026.");
  assert.equal(v.pay, "offer");
});

test("a lifetime customer owes nothing and sees no pay option", () => {
  const v = subscriptionsView(ready(options({ state: "legacy_lifetime" })), { has_paid: true });
  assert.equal(v.kind, "legacy");
  assert.equal(v.pay, "none");
});

test("with subscription billing off the page keeps the old one-time activation: one Activate button for an account that is not activated, nothing for one that is", () => {
  const off = { status: "off", options: null };
  assert.equal(subscriptionsView(off, { has_paid: false }).pay, "legacy");
  assert.equal(subscriptionsView(off, { has_paid: true }).pay, "none");
  assert.equal(subscriptionsView(off, { has_paid: true }).kind, "activated");
  assert.equal(subscriptionsView(off, { has_paid: false }).kind, "activate");
  assert.equal(subscriptionsView({ status: "loading", options: null }, null).kind, "loading");
});

test("date lines say nothing when the server sent no date", () => {
  assert.equal(dateLine(options({ state: "active" })), null);
  assert.equal(dateLine(options({ state: "cancelled" })), null);
  assert.equal(dateLine(null), null);
});

test("the Dashboard line points to Subscriptions and carries no price or button", () => {
  assert.match(dashboardLine({ status: "off", options: null }), /Subscriptions/);
  assert.match(dashboardLine(ready(options({ state: "expired" }))), /ended/);
  assert.ok(!/\d/.test(dashboardLine(ready(options()))));
});

test("the sidebar has a Subscriptions tab and the app maps the route to its page", () => {
  assert.deepEqual(NAV_ITEMS.find((i) => i.route === "subscriptions"), { route: "subscriptions", label: "Subscriptions" });
  assert.ok(src("../App.jsx").includes("subscriptions: Subscriptions"));
  assert.ok(src("./navIconPaths.js").includes("subscriptions:"));
});

test("the Dashboard no longer carries the payment options", () => {
  const dash = src("../pages/dashboard/TestnetDashboard.jsx");
  assert.ok(!dash.includes("SubscribeCard") && !dash.includes("PayButtons") && !dash.includes("onActivate"));
  assert.ok(dash.includes("{dashboardLine(billing)}") && dash.includes("navigate(SUBSCRIPTIONS_ROUTE)"));
  const router = src("../pages/DashboardRouter.jsx");
  assert.ok(!router.includes("/billing/initialize") && !router.includes("onActivate"));
  assert.ok(!src("../components/SubscriptionStatusCard.jsx").includes("PayButtons"));
});

test("the Subscriptions page calls only the existing activation route (the pay buttons call the existing checkout routes)", () => {
  const page = src("../pages/Subscriptions.jsx");
  const calls = [...page.matchAll(/api\.(get|post|put|delete)\(\s*["']([^"']+)["']/g)].map((m) => `${m[1]} ${m[2]}`);
  assert.deepEqual(calls, ["post /billing/initialize"]);
});
