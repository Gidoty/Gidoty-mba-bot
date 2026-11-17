import assert from "node:assert/strict";
import test from "node:test";

import { SUGGESTED_PCT, TABS, dashboardNotice, limitProblem, nextTabIndex, oneTradeCanExceed, pctOfValue, pendingNotice, suggestedText, tabForMessage } from "./lossLimit.js";

test("the amount must be above zero and below the total stop level", () => {
  assert.equal(limitProblem("40", 500), null);
  assert.equal(limitProblem("499.99", 500), null);
  for (const bad of ["0", "-3", "abc", "NaN"]) assert.match(limitProblem(bad, 500), /above 0/, bad);
  assert.match(limitProblem("500", 500), /below \$500, the level where the automatic total stop acts \(50% of your account value\)/);
  assert.match(limitProblem("900", 500), /below/);
  assert.equal(limitProblem("900", null), null); // no reference yet: only the above-zero rule can apply
});

test("empty is a problem only when a limit is required", () => {
  assert.equal(limitProblem("", 500), null);
  assert.match(limitProblem("", 500, { required: true }), /required before live trading/);
});

test("the percent next to the amount", () => {
  assert.equal(pctOfValue("50", 1000), 5);
  assert.equal(pctOfValue("40", 1000), 4);
  assert.equal(pctOfValue("33.3", 1000), 3.3);
  assert.equal(pctOfValue("", 1000), null);
  assert.equal(pctOfValue("50", null), null);
  assert.equal(pctOfValue("50", 0), null);
});

test("the suggestion is about 5% of the account value", () => {
  assert.equal(SUGGESTED_PCT, 5);
  assert.equal(suggestedText(1000), "$50");
  assert.equal(suggestedText(null), null);
  assert.equal(suggestedText(0), null);
});

test("one trade can lose more than the limit: risk x capital above the amount", () => {
  assert.equal(oneTradeCanExceed(5, 1000, 40), true); // 50 > 40
  assert.equal(oneTradeCanExceed(5, 1000, 50), false);
  assert.equal(oneTradeCanExceed(1, 1000, 40), false);
  assert.equal(oneTradeCanExceed("", 1000, 40), false);
  assert.equal(oneTradeCanExceed(5, null, 40), false);
});

test("a pending raise says when it starts", () => {
  assert.equal(pendingNotice({ daily_loss_limit_pending_usd: 90 }), "Your new limit of $90 starts at midnight Lagos time.");
  assert.equal(pendingNotice({ daily_loss_limit_pending_usd: null }), null);
  assert.equal(pendingNotice({}), null);
});

test("a server message lands on the tab it belongs to", () => {
  assert.equal(tabForMessage("Your daily loss limit must be below 500.00"), "risk");
  assert.equal(tabForMessage("Risk per trade can be at most 2% on this service."), "risk");
  assert.equal(tabForMessage("Leverage must be a whole number"), "risk");
  assert.equal(tabForMessage("pair BTC/USDT is not available with your trading capital"), "trading");
  assert.equal(tabForMessage("cannot change pair, market type, or pair selection mode while a trade is open"), "trading");
  assert.equal(tabForMessage(""), "risk");
});

test("arrow keys move between the two tabs and wrap; Home and End jump", () => {
  assert.deepEqual(TABS.map((t) => t.id), ["trading", "risk"]);
  assert.equal(nextTabIndex("ArrowRight", 0, 2), 1);
  assert.equal(nextTabIndex("ArrowRight", 1, 2), 0);
  assert.equal(nextTabIndex("ArrowLeft", 0, 2), 1);
  assert.equal(nextTabIndex("Home", 1, 2), 0);
  assert.equal(nextTabIndex("End", 0, 2), 1);
  assert.equal(nextTabIndex("Tab", 0, 2), null);
  assert.equal(nextTabIndex("a", 0, 2), null);
});

test("the dashboard says so while the daily limit is the service's starting amount, and when a live account has none", () => {
  const d = dashboardNotice({ daily_loss_limit_is_default: true, daily_loss_limit_usd: 100 });
  assert.equal(d.kind, "default");
  assert.match(d.text, /Your daily loss limit is \$100\. We set this starting amount for you/);
  assert.equal(dashboardNotice({ daily_loss_limit_is_default: false, daily_loss_limit_usd: 100 }), null); // the customer has chosen: nothing to show
  const r = dashboardNotice({ daily_loss_limit_required: true, live_trading_enabled_at: "2026-11-01T00:00:00Z" });
  assert.equal(r.kind, "required");
  assert.match(r.text, /opens no new trades until you do/);
  assert.equal(dashboardNotice({ daily_loss_limit_required: true, live_trading_enabled_at: null }), null); // a practice account does not need one yet
  assert.equal(dashboardNotice(null), null);
});
