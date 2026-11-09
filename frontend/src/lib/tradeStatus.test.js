import assert from "node:assert/strict";
import test from "node:test";

import { openStatus, tradeToPosition } from "./tradeStatus.js";

const trade = { side: "long", entry_price: 100, stop_loss_price: 95, quantity: 2, opened_at: "2026-11-07T21:05:00Z", is_paper: false, protection_status: "protected" };

test("every open status has customer wording; unknown falls back to Open", () => {
  assert.equal(openStatus("pending").label, "Placing the entry");
  assert.equal(openStatus("protecting").tone, "warn");
  assert.equal(openStatus("attention").label, "Needs attention");
  assert.equal(openStatus("closing").label, "Closing");
  assert.equal(openStatus("zzz").label, "Open");
});

test("live trade takes the current price and unrealised result from the live position", () => {
  const p = tradeToPosition(trade, { has_position: true, current_price: 103, unrealized_pnl: 6, protected: true });
  assert.equal(p.current_price, 103);
  assert.equal(p.unrealized_pnl, 6);
  assert.equal(p.protected, true);
});

test("without a live position the card gets no invented price", () => {
  const p = tradeToPosition(trade, { has_position: false });
  assert.equal(p.current_price, null);
  assert.equal(p.unrealized_pnl, null);
  assert.equal(p.protected, true); // from the trade's own protection_status
  assert.equal(tradeToPosition({ ...trade, protection_status: "pending" }, null).protected, false);
});

test("a practice trade never shows live data or a protection claim", () => {
  const p = tradeToPosition({ ...trade, is_paper: true }, { has_position: true, current_price: 103, unrealized_pnl: 6, protected: true });
  assert.equal(p.current_price, null);
  assert.equal(p.unrealized_pnl, null);
  assert.equal(p.protected, null);
});
