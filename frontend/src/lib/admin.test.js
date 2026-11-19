import assert from "node:assert/strict";
import test from "node:test";

import { adminStage, ageText, codeProblem, pageCount, reasonProblem, resultLine, winRate } from "./admin.js";

test("the page shows only what /admin/me allows", () => {
  assert.equal(adminStage(null), "hidden");
  assert.equal(adminStage(undefined), "hidden");
  assert.equal(adminStage({}), "hidden");
  assert.equal(adminStage({ is_admin: false, session: false }), "hidden");
  assert.equal(adminStage({ is_admin: false, session: true }), "hidden");
  assert.equal(adminStage({ is_admin: true, session: false }), "login");
  assert.equal(adminStage({ is_admin: true, session: true }), "dashboard");
  assert.equal(adminStage({ is_admin: "yes", session: true }), "hidden");
});

test("the code box accepts 6 digits or a recovery code and nothing else", () => {
  assert.equal(codeProblem("123456"), null);
  assert.equal(codeProblem("123 456"), null);
  assert.equal(codeProblem("ABCD-EFGH-JKLM-NPQR"), null);
  assert.match(codeProblem(""), /6-digit/);
  assert.match(codeProblem("12345"), /6 digits/);
  assert.match(codeProblem("1234567"), /6 digits/);
  assert.match(codeProblem("ab!cd"), /does not look/);
  assert.match(codeProblem("A".repeat(17)), /does not look/);
});

test("a reveal reason is 5 to 200 characters after trimming", () => {
  assert.notEqual(reasonProblem("four"), null);
  assert.notEqual(reasonProblem("     "), null);
  assert.equal(reasonProblem("support ticket 12"), null);
  assert.equal(reasonProblem("x".repeat(200)), null);
  assert.notEqual(reasonProblem("x".repeat(201)), null);
});

test("ages are readable", () => {
  assert.equal(ageText(null), "never");
  assert.equal(ageText(30), "30 s ago");
  assert.equal(ageText(600), "10 min ago");
  assert.equal(ageText(7200), "2 h ago");
  assert.equal(ageText(86400 * 3), "3 days ago");
  assert.equal(ageText(-1), "never");
});

test("a result is worded exactly as the server labelled it, and net only when the server sent one", () => {
  assert.equal(resultLine({ simulated: true, gross_pnl: 3.456, net_pnl: null }), "+3.46 USDT (simulated, no fees or slippage)");
  assert.equal(resultLine({ simulated: false, gross_pnl: -1, net_pnl: null }), "-1.00 USDT before fees");
  assert.equal(resultLine({ simulated: false, gross_pnl: 5, net_pnl: 4.5 }), "+4.50 USDT net after fees (+5.00 before fees)");
  assert.equal(resultLine({ simulated: true, gross_pnl: 1, net_pnl: 0.5 }), "+1.00 USDT (simulated, no fees or slippage)");
});

test("win rate and paging", () => {
  assert.equal(winRate({ wins: 3, losses: 1 }), 75);
  assert.equal(winRate({ wins: 0, losses: 0 }), null);
  assert.equal(pageCount(122, 50), 3);
  assert.equal(pageCount(0, 50), 1);
});
