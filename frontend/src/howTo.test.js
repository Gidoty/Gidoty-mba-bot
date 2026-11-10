// Run with: node --test frontend/src/howTo.test.js (also run by tests/test_frontend_lib.py)
import assert from "node:assert/strict";
import test from "node:test";

import { howToSections } from "./howTo.js";

const sections = howToSections("203.0.113.9");
const all = sections.flatMap((s) => s.steps.map(([title, body]) => `${title}. ${body}`));

test("the walk-through follows the order a new customer meets the app", () => {
  const titles = sections.flatMap((s) => s.steps.map(([t]) => t));
  const order = ["Sign up", "Link Telegram", "Try practice mode", "Activate when you are ready", "Create a Binance API key", "Add the server address", "Connect Binance", "Dashboard", "Positions", "Trade History", "Performance", "Bot Settings", "Notifications", "Account", "Enable live trading", "Set your capital and start the bot", "Stopping"];
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
});
