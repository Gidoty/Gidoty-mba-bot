// Run with: node --test frontend/src/howTo.test.js (also run by tests/test_frontend_lib.py)
import assert from "node:assert/strict";
import test from "node:test";

import { howToSections } from "./howTo.js";

const sections = howToSections("203.0.113.9");
const all = sections.flatMap((s) => s.steps.map(([title, body]) => `${title}. ${body}`));

test("the walk-through follows the order a new customer meets the app", () => {
  const titles = sections.flatMap((s) => s.steps.map(([t]) => t));
  const order = ["Sign up", "Link Telegram", "Try practice mode", "Activate when you are ready", "Create a Binance API key", "Add the server address", "Connect Binance", "Dashboard", "Positions", "Trade History", "Performance", "Bot Settings", "Notifications", "Account", "Risk per trade", "Choosing your risk", "Leverage on Futures", "Risk and leverage compared", "The liquidation check", "Daily loss limit", "Total loss stop", "Open trades and your money", "Enable live trading", "Set your capital and start the bot", "Stopping"];
  assert.deepEqual(titles, order);
});

test("steps are short and plain", () => {
  for (const [title, body] of sections.flatMap((s) => s.steps)) {
    assert.ok(title.length < 50 && body.length > 20 && body.length < 420, title);
  }
});

test("it says what the owner asked: trade-only key, withdrawals off, the server address, before fees, simulated", () => {
  const text = all.join(" ");
  assert.match(text, /withdrawals and transfers switched off/);
  assert.match(text, /203\.0\.113\.9/);
  assert.match(text, /before exchange fees and funding/);
  assert.match(text, /simulated/);
  assert.match(text, /There is no Pause/);
});

test("no promise of profit and none of the removed risk notices", () => {
  const text = all.join(" ").toLowerCase();
  for (const bad of ["guarantee", "will make", "earn ", "passive income", "risk-free", "results are not proven", "trading can lose money", "afford to lose"]) {
    assert.ok(!text.includes(bad), bad);
  }
});

test("money is shown as dollars, never a bare currency code, in the settings step", () => {
  const settings = all.find((s) => s.startsWith("Bot Settings"));
  assert.match(settings, /in dollars/);
});

test("what cannot change while a trade is open is exactly pair, market and pair selection mode (account.py)", () => {
  const settings = all.find((s) => s.startsWith("Bot Settings"));
  assert.match(settings, /Pair, market and pair selection mode cannot be changed while a trade is open/);
  assert.match(settings, /Risk per trade and trading capital can be changed at any time/);
  assert.ok(!/risk[^.]*cannot be changed while a trade is open/i.test(settings));
});

test("replacing the key and enabling live trading are both refused while a trade is open, and the steps say so", () => {
  assert.match(all.find((s) => s.startsWith("Stopping")), /cannot be done while a trade is open/);
  assert.match(all.find((s) => s.startsWith("Enable live trading")), /cannot be done while a practice trade is open/);
  assert.match(all.find((s) => s.startsWith("Set your capital and start the bot")), /Start Bot cannot be pressed while a practice trade is open/); // account.py line 449
});

test("risk per trade is defined as the loss if the stop-loss is hit, with the 100 USDT example, and is not the size of the trade", () => {
  const def = all.find((x) => x.startsWith("Risk per trade."));
  assert.match(def, /share of your trading capital the bot is willing to lose on one trade if its stop-loss is hit/);
  assert.match(def, /not the amount put into the trade and it is not the size of the trade/);
  assert.match(def, /100 USDT of trading capital and a risk of 2%.*loses about 2 USDT/);
  assert.match(def, /The trade itself can be worth more than 2 USDT/);
});

test("choosing your risk: up to 40%, recommended 2% or less, the red band from 10%, the two ticks, a worse fill; no line says 2% is the maximum", () => {
  const risk = all.find((x) => x.startsWith("Choosing your risk."));
  assert.match(risk, /from 0\.1% up to 40% at most/);
  assert.match(risk, /We recommend 2% or less/);
  assert.match(risk, /Above 2% the screen warns you and asks you to tick a box/);
  assert.match(risk, /From 10% it turns red and asks for a second, stronger tick/);
  assert.match(risk, /If a stop fills at a worse price than planned, the loss can be larger/);
  const text = all.join(" ");
  for (const old of [/at most 2%/i, /maximum[^.]{0,40}2%/i, /capped at 2%/i, /between 0\.1% and 2%/i, /0\.1% to 2%/i, /2% maximum/i, /up to 2%/i, /hard cap/i]) {
    assert.ok(!old.test(text), String(old));
  }
});

test("risk and leverage are told apart, and changes apply to new trades only", () => {
  const both = all.find((x) => x.startsWith("Risk and leverage compared."));
  assert.match(both, /risk sets how much a stop-loss hit may lose, and the leverage setting caps how large the trade can be compared with your trading capital/);
  assert.match(both, /applies to new trades only/);
});

test("leverage: 1x to 5x, 3x by default, dashboard only, the smaller number, nothing changed on Binance, isolated margin", () => {
  const lev = all.find((x) => x.startsWith("Leverage on Futures."));
  assert.match(lev, /1x to 5x, 3x until you change it/);
  assert.match(lev, /on the dashboard only/);
  assert.match(lev, /smaller of the two/);
  assert.match(lev, /never changes anything on Binance/);
  assert.match(lev, /isolated margin only/);
});

test("the safety limits are described as they work: new trades only, daily loss limit on Lagos time, total loss stop reset by the customer, liquidation check can refuse", () => {
  const daily = all.find((x) => x.startsWith("Daily loss limit."));
  assert.match(daily, /opens no new trades until the next day starts/);
  assert.match(daily, /midnight Lagos time/);
  assert.match(daily, /You set your own daily loss limit in US dollars \(USDT\)/);
  assert.match(daily, /Lowering it applies at once; raising it starts at the next midnight Lagos time/);
  assert.match(daily, /does not change with your risk setting/);
  const total = all.find((x) => x.startsWith("Total loss stop."));
  assert.match(total, /50% or more below its reference value/);
  assert.match(total, /until you reset it on Bot Settings/);
  assert.match(total, /never resets on its own/);
  assert.match(total, /automatic and the same for every account/);
  assert.match(all.find((x) => x.startsWith("The liquidation check.")), /does not open the trade and tells you/);
  const money = all.find((x) => x.startsWith("Open trades and your money."));
  assert.match(money, /Do not deposit or withdraw money in the middle of the day/);
  assert.match(money, /only stop new trades/);
});

test("the Help page and the Landing page use the same risk text and do not say 2% is the maximum", async () => {
  const { RISK_HELP_TOPICS } = await import("./riskGuideText.js");
  assert.equal(RISK_HELP_TOPICS.length, 4);
  assert.ok(RISK_HELP_TOPICS[0].body.includes("up to 40% at most") && RISK_HELP_TOPICS[0].body.includes("not the size of the trade"));
  const fs = await import("node:fs");
  const landing = fs.readFileSync(new URL("./pages/Landing.jsx", import.meta.url), "utf8");
  assert.match(landing, /never above 40%/);
  assert.ok(!/Set 0\.1% to 2%/.test(landing) && !/at most 2%/.test(landing));
});

test("Bot Settings has two tabs and the daily loss limit; the theme control is on Notifications, top right, and no longer under Bot Settings", () => {
  const settings = all.find((x) => x.startsWith("Bot Settings."));
  assert.match(settings, /Two tabs, one Save/);
  assert.match(settings, /Trading: pair selection, pair, market and trading capital/);
  assert.match(settings, /Risk: risk per trade .*leverage on Futures and your daily loss limit, required before live trading/);
  assert.ok(!/Appearance/.test(all.join(" ")));
  const notifications = all.find((x) => x.startsWith("Notifications."));
  assert.match(notifications, /Theme control in the top right corner sets Auto, Light or Dark/);
});
