// Run with: node --test frontend/src/lib/riskBands.test.js (also run by tests/test_frontend_lib.py)
import assert from "node:assert/strict";
import test from "node:test";

import { ACK_TEXT, BAND_INFO, acknowledgmentsFor, riskBand, riskProblem } from "./riskBands.js";

test("bands at the boundaries", () => {
  const cases = [[0.1, "recommended"], [1, "recommended"], [2, "recommended"], [2.01, "high"], [5, "high"], [9.99, "high"], [10, "very_high"], [20, "very_high"], [40, "very_high"]];
  for (const [v, band] of cases) assert.equal(riskBand(v), band, String(v));
  assert.equal(riskBand("abc"), "invalid");
  assert.equal(riskBand("2"), "recommended");
});

test("every band has a text label and an icon, so colour is never the only signal", () => {
  for (const b of ["recommended", "high", "very_high"]) {
    assert.ok(BAND_INFO[b].label.length > 5 && BAND_INFO[b].icon.length > 2, b);
  }
  assert.match(BAND_INFO.recommended.label, /Recommended: 2% or less/);
  assert.match(BAND_INFO.high.label, /Higher risk/);
  assert.match(BAND_INFO.very_high.label, /Very high risk: a short run of losing trades can remove most of your capital/);
  assert.equal(BAND_INFO.recommended.tone, "pos");
  assert.equal(BAND_INFO.high.tone, "warn");
  assert.equal(BAND_INFO.very_high.tone, "neg");
});

test("acknowledgments: none up to 2%, one above 2%, two from 10%", () => {
  assert.deepEqual(acknowledgmentsFor(2), []);
  assert.deepEqual(acknowledgmentsFor(2.01), ["high"]);
  assert.deepEqual(acknowledgmentsFor(9.99), ["high"]);
  assert.deepEqual(acknowledgmentsFor(10), ["high", "very_high"]);
  assert.deepEqual(acknowledgmentsFor(40), ["high", "very_high"]);
  assert.deepEqual(acknowledgmentsFor(1), []);
  assert.ok(ACK_TEXT.high.length > 40 && ACK_TEXT.very_high.length > 40);
});

test("the range check uses the limit from the server", () => {
  assert.equal(riskProblem(0.1, 2), null);
  assert.equal(riskProblem(2, 2), null);
  assert.match(riskProblem(2.01, 2), /at most 2%/);
  assert.equal(riskProblem(40, 40), null);
  assert.match(riskProblem(40.01, 40), /at most 40%/);
  assert.match(riskProblem(0.09, 40), /at least 0.1%/);
  assert.match(riskProblem("", 40), /number/);
});
