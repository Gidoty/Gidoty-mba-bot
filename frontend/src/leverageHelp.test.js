// Run with: node --test frontend/src/leverageHelp.test.js (also run by tests/test_frontend_lib.py)
// The help texts on the dashboard must be exactly the approved sections of docs/customer/leverage_help.md, and must not contain anything the doc marks UNVERIFIED.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { LEVERAGE_HELP, MARGIN_MODE_HELP } from "./leverageHelp.js";

const doc = readFileSync(new URL("../../docs/customer/leverage_help.md", import.meta.url), "utf8");

function section(n) {
  const a = doc.indexOf(`## ${n}. `);
  const b = doc.indexOf("\n## ", a + 5);
  return doc.slice(a, b).split("\n").slice(1).join("\n").trim();
}

function blocks(text) {
  return text.split(/\n\s*\n/).map((chunk) => {
    const lines = chunk.split("\n");
    const m = lines[0].match(/^\*\*(.+?)\*\*\s*(.*)/);
    const bullets = lines.slice(1).filter((l) => l.startsWith("- ")).map((l) => l.slice(2).trim());
    const rest = lines.slice(1).filter((l) => !l.startsWith("- "));
    const b = m
      ? { title: m[1].replace(/\.$/, ""), text: [m[2], ...rest].join(" ").trim() }
      : { title: null, text: [lines[0], ...rest].join(" ").trim() };
    if (bullets.length) b.bullets = bullets;
    return b;
  });
}

test("the leverage help is exactly section 1 of the approved doc", () => {
  assert.deepEqual(LEVERAGE_HELP.blocks, blocks(section(1)));
});

test("the margin mode help is exactly section 2 of the approved doc", () => {
  assert.deepEqual(MARGIN_MODE_HELP.blocks, blocks(section(2)));
});

test("nothing marked UNVERIFIED, no exact Binance control name and no rule about changing margin mode with a position open reaches customers", () => {
  const all = JSON.stringify([LEVERAGE_HELP, MARGIN_MODE_HELP]);
  for (const forbidden of [/UNVERIFIED/i, /Margin Mode control/, /Leverage control/, /cannot change margin mode/i, /can't change margin mode/i, /while you hold a position/i, /while a position is open/i]) {
    assert.ok(!forbidden.test(all), String(forbidden));
  }
  assert.match(all, /open the Futures page for the pair on Binance and look for the Leverage and Margin Mode settings/);
});

test("the text is plain: no markdown marks, and the key sentences are there", () => {
  const all = JSON.stringify([LEVERAGE_HELP, MARGIN_MODE_HELP]);
  assert.ok(!all.includes("**") && !all.includes("`"));
  assert.match(all, /21\.60 x 3 = 64\.80 USDT/);
  assert.match(all, /The bot never changes anything on Binance/);
  assert.match(all, /lose all the money set aside for it/);
  assert.match(all, /This service trades isolated margin only/);
});
