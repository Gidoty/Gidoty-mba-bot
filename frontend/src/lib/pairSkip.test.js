import assert from "node:assert/strict";
import test from "node:test";

import { pairSkipNotice } from "./pairSkip.js";

test("the notice is the server's text, or nothing", () => {
  const text = "BTC/USDT is too small for your trading capital. Its smallest order needs about 33.34 USDT of trading capital at your 3x leverage limit.";
  assert.equal(pairSkipNotice({ pair_skip_message: text }), text);
  assert.equal(pairSkipNotice({ pair_skip_message: null }), null);
  assert.equal(pairSkipNotice({ pair_skip_message: "" }), null);
  assert.equal(pairSkipNotice({}), null);
  assert.equal(pairSkipNotice(null), null);
});
