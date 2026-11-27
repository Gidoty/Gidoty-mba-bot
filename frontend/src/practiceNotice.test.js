// Run with: node --test frontend/src/practiceNotice.test.js (also run by tests/test_frontend_lib.py)
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { PRACTICE_NOTICE_HELP, PRACTICE_NOTICE_LABEL, PRACTICE_NOTICE_TEXT } from "./practiceNotice.js";

test("the full practice notice wording is exactly the paragraph the Dashboard showed before", () => {
  assert.equal(
    PRACTICE_NOTICE_TEXT,
    "Everything on this page is simulated. The bot reads prices from Binance's practice server, then records each practice trade itself. No order is sent to Binance, no real money is involved, and nothing here can be withdrawn. Real trades can fill at different prices and pay fees."
  );
});

test("the label is short and the help carries the whole text as one block", () => {
  assert.equal(PRACTICE_NOTICE_LABEL, "Practice mode");
  assert.equal(PRACTICE_NOTICE_HELP.title, "Practice mode");
  assert.deepEqual(PRACTICE_NOTICE_HELP.blocks, [{ text: PRACTICE_NOTICE_TEXT }]);
});

test("the Dashboard no longer prints the paragraph itself and uses the info button instead", () => {
  const src = readFileSync(new URL("./pages/dashboard/TestnetDashboard.jsx", import.meta.url), "utf8");
  assert.ok(!src.includes("Everything on this page is simulated"), "the paragraph must not be inline on the Dashboard");
  assert.ok(src.includes("<InfoHelp") && src.includes("PRACTICE_NOTICE_HELP") && src.includes("PRACTICE_NOTICE_LABEL"));
  assert.ok(src.includes('label="practice mode"'), "the button needs an accessible name (InfoHelp builds: More information: practice mode)");
});

test("no other page prints this paragraph (the Practice page keeps its own visible list)", () => {
  for (const f of ["./pages/Practice.jsx", "./pages/Positions.jsx", "./pages/Account.jsx", "./components/ui.jsx"]) {
    assert.ok(!readFileSync(new URL(f, import.meta.url), "utf8").includes("Everything on this page is simulated"), f);
  }
});
