import assert from "node:assert/strict";
import test from "node:test";

import { fmtPrice, positionView, priceDecimals, sharedDecimals } from "./position.js";

test("decimals scale with the price", () => {
  assert.equal(priceDecimals(104250), 0);
  assert.equal(priceDecimals(1000), 0);
  assert.equal(priceDecimals(620.4), 2);
  assert.equal(priceDecimals(1), 2);
  assert.equal(priceDecimals(0.45), 4);
  assert.equal(priceDecimals(0.01), 4);
  assert.equal(priceDecimals(0.0009), 6);
  assert.equal(priceDecimals(0.0099), 6);
  assert.equal(priceDecimals(0.005), 6);
  assert.equal(priceDecimals(0.0123), 4);
  assert.equal(priceDecimals(999.99), 2);
  assert.equal(priceDecimals(0), 2);
  assert.equal(priceDecimals(null), 2);
});

test("BTC (104250): whole numbers, distance reads in whole numbers", () => {
  const v = positionView({ side: "long", entry_price: 104250, stop_loss_price: 102980, current_price: 105340, quantity: 0.079 });
  assert.equal(v.decimals, 0);
  assert.equal(fmtPrice(v.distance, v.decimals), "2,360");
  assert.equal(fmtPrice(102980, v.decimals), "102,980");
  assert.equal(v.distancePct.toFixed(2), "2.24");
});

test("BNB (620.4): two decimals, labels stay distinct", () => {
  const v = positionView({ side: "long", entry_price: 620.4, stop_loss_price: 612.9, current_price: 624.15, quantity: 1.5 });
  assert.equal(v.decimals, 2);
  assert.equal(fmtPrice(v.distance, v.decimals), "11.25");
  const labels = [620.4, 612.9, 624.15].map((p) => fmtPrice(p, v.decimals));
  assert.deepEqual(labels, ["620.40", "612.90", "624.15"]);
  assert.equal(new Set(labels).size, 3);
});

test("0.45 pair: four decimals, stop distance is not 0", () => {
  const v = positionView({ side: "long", entry_price: 0.45, stop_loss_price: 0.4392, current_price: 0.4561, quantity: 1000 });
  assert.equal(v.decimals, 4);
  assert.equal(fmtPrice(v.distance, v.decimals), "0.0169");
  assert.notEqual(fmtPrice(v.distance, v.decimals), "0");
  assert.equal(new Set([0.45, 0.4392, 0.4561].map((p) => fmtPrice(p, v.decimals))).size, 3);
});

test("0.0009 pair: six decimals, stop distance is not 0", () => {
  const v = positionView({ side: "long", entry_price: 0.0009, stop_loss_price: 0.000888, current_price: 0.000915, quantity: 1000000 });
  assert.equal(v.decimals, 6);
  assert.equal(fmtPrice(v.distance, v.decimals), "0.000027");
  assert.equal(new Set([0.0009, 0.000888, 0.000915].map((p) => fmtPrice(p, v.decimals))).size, 3);
});

test("one shared decimals value, taken from the smallest price", () => {
  assert.equal(sharedDecimals(1200, 0.5), 4);
  assert.equal(sharedDecimals(null, undefined), 2);
  assert.equal(sharedDecimals(104250, null, 102980), 0);
});

test("long: loss if stopped is negative; beyond the stop only when price is at or under it", () => {
  const ok = positionView({ side: "long", entry_price: 100, stop_loss_price: 95, current_price: 102, quantity: 2 });
  assert.equal(ok.pnlIfStopped, -10);
  assert.equal(ok.beyondStop, false);
  const atStop = positionView({ side: "long", entry_price: 100, stop_loss_price: 95, current_price: 95, quantity: 2 });
  assert.equal(atStop.beyondStop, true);
  const under = positionView({ side: "long", entry_price: 100, stop_loss_price: 95, current_price: 94, quantity: 2 });
  assert.equal(under.beyondStop, true);
  assert.equal(under.distance, 1);
});

test("short: loss if stopped is negative; beyond the stop only when price is at or over it", () => {
  const ok = positionView({ side: "short", entry_price: 100, stop_loss_price: 105, current_price: 98, quantity: 2 });
  assert.equal(ok.pnlIfStopped, -10);
  assert.equal(ok.beyondStop, false);
  const over = positionView({ side: "short", entry_price: 100, stop_loss_price: 105, current_price: 106, quantity: 2 });
  assert.equal(over.beyondStop, true);
  const wrongWay = positionView({ side: "short", entry_price: 100, stop_loss_price: 95, current_price: 98, quantity: 2 });
  // A short with its stop BELOW entry would lock in a gain (positive result). A short's stop triggers when price
  // rises to it, and 98 is already above 95, so it counts as beyond the stop.
  assert.equal(wrongWay.pnlIfStopped, 10);
  assert.equal(wrongWay.beyondStop, true);
});

test("a long and a short of the same size and distance lose the same amount", () => {
  const l = positionView({ side: "LONG", entry_price: 50, stop_loss_price: 48, current_price: 51, quantity: 3 });
  const s = positionView({ side: "SHORT", entry_price: 50, stop_loss_price: 52, current_price: 49, quantity: 3 });
  assert.equal(l.pnlIfStopped, -6);
  assert.equal(s.pnlIfStopped, -6);
});

test("missing numbers stay null and nothing throws (practice trades have no live price)", () => {
  const v = positionView({ side: "long", entry_price: 100, stop_loss_price: 98, current_price: null, quantity: 1, unrealized_pnl: null });
  assert.equal(v.distance, null);
  assert.equal(v.distancePct, null);
  assert.equal(v.beyondStop, false);
  assert.equal(v.pnlIfStopped, -2);
  const none = positionView({ side: "short" });
  assert.equal(none.pnlIfStopped, null);
  assert.equal(none.decimals, 2);
});

test("stop side follows the real prices, including after the price has crossed the stop", () => {
  assert.equal(positionView({ side: "long", entry_price: 100, stop_loss_price: 95, current_price: 102 }).stopSide, "below");
  assert.equal(positionView({ side: "long", entry_price: 100, stop_loss_price: 95, current_price: 94 }).stopSide, "above");
  assert.equal(positionView({ side: "short", entry_price: 100, stop_loss_price: 105, current_price: 98 }).stopSide, "above");
  assert.equal(positionView({ side: "short", entry_price: 100, stop_loss_price: 105, current_price: 106 }).stopSide, "below");
  assert.equal(positionView({ side: "long", entry_price: 100, stop_loss_price: 95 }).stopSide, null);
});
