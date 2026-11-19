import assert from "node:assert/strict";
import test from "node:test";

import { capText, listToSave, maxExtraPairs, sameList, toggleExtra } from "./livePairs.js";

test("extra pairs: none with one trade, slots minus one otherwise, never more than two", () => {
  assert.deepEqual([1, 2, 3, 4, "x", null].map(maxExtraPairs), [0, 1, 2, 2, 0, 0]);
});

test("ticking adds, ticking again removes, the main pair and a full list are refused", () => {
  const o = { slots: 3, primary: "BTC/USDT" };
  assert.deepEqual(toggleExtra([], "ETH/USDT", o), ["ETH/USDT"]);
  assert.deepEqual(toggleExtra(["ETH/USDT"], "ETH/USDT", o), []);
  assert.deepEqual(toggleExtra(["ETH/USDT"], "BTC/USDT", o), ["ETH/USDT"]);
  assert.deepEqual(toggleExtra(["ETH/USDT", "BNB/USDT"], "XRP/USDT", o), ["ETH/USDT", "BNB/USDT"]);
  assert.deepEqual(toggleExtra(["ETH/USDT"], "BNB/USDT", { slots: 2, primary: "BTC/USDT" }), ["ETH/USDT"]);
});

test("Save sends the list cut to the slots, without the main pair or duplicates", () => {
  assert.deepEqual(listToSave(["ETH/USDT", "BNB/USDT"], { slots: 2, primary: "BTC/USDT" }), ["ETH/USDT"]);
  assert.deepEqual(listToSave(["ETH/USDT"], { slots: 1, primary: "BTC/USDT" }), []);
  assert.deepEqual(listToSave(["BTC/USDT", "ETH/USDT", "ETH/USDT"], { slots: 3, primary: "BTC/USDT" }), ["ETH/USDT"]);
  assert.deepEqual(listToSave(null, { slots: 3, primary: "BTC/USDT" }), []);
});

test("lists compare in order", () => {
  assert.equal(sameList(["A", "B"], ["A", "B"]), true);
  assert.equal(sameList(["A", "B"], ["B", "A"]), false);
  assert.equal(sameList(null, []), true);
});

test("the cap text uses the service's number and says it cannot be changed", () => {
  const t = capText({ capPct: 3, riskPct: 1, slots: 3, capital: 1000 });
  assert.match(t, /3% of your trading capital \(.*30.*\)/);
  assert.match(t, /cannot change this cap/);
  assert.match(t, /3 trades together risk up to 3%/);
  assert.equal(capText({ capPct: null, riskPct: 1, slots: 3, capital: 1000 }), null);
  assert.doesNotMatch(capText({ capPct: 3, riskPct: 1, slots: 1, capital: 0 }), /trades together/);
});
