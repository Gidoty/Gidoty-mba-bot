import test from "node:test";
import assert from "node:assert/strict";
import { buildRequest } from "./advice.js";

const base = { account: { pair: "BTC/USDT", market_type: "futures", pair_selection_mode: "manual", max_leverage: 3, live_slots: 1 }, pair: "BTC/USDT", marketType: "futures", riskPct: "1", pairSelectionMode: "manual", maxLeverage: "3", capitalInput: "1000", limitInput: "", liveSlots: 1, acks: {}, savedCapital: "1000", savedLimit: "" };

test("the candle-size preview is sent only for the three sizes the server knows", () => {
  assert.equal(buildRequest({ ...base, previewTimeframe: "4h" }).ema_timeframe, "4h");
  assert.equal(buildRequest({ ...base, previewTimeframe: "1d" }).ema_timeframe, "1d");
  for (const bad of ["", undefined, null, "7m", "1H", "4h; drop"]) assert.equal("ema_timeframe" in buildRequest({ ...base, previewTimeframe: bad }), false, String(bad));
});

test("without a preview the request is exactly what it was before", () => {
  assert.deepEqual(buildRequest(base), buildRequest({ ...base, previewTimeframe: "" }));
});
