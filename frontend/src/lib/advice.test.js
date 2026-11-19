import assert from "node:assert/strict";
import test from "node:test";

import { MAX_DASHBOARD_NOTES, buildRequest, dashboardNotes, notesForTab, requestKey, sortNotes } from "./advice.js";

const account = { pair: "BTC/USDT", market_type: "futures", pair_selection_mode: "manual", max_leverage: 3, live_slots: 1, live_multi_available: true };
const form = { account, pair: "BTC/USDT", marketType: "futures", riskPct: "1", pairSelectionMode: "manual", maxLeverage: "3", capitalInput: "100", limitInput: "5", liveSlots: "1", acks: {}, savedCapital: "100", savedLimit: "5" };

test("an unchanged form asks only about the risk setting", () => {
  assert.deepEqual(buildRequest(form), { risk_pct: 1 });
});

test("only changed fields are sent, like Save", () => {
  const body = buildRequest({ ...form, pair: "ETH/USDT", capitalInput: "50", limitInput: "9", liveSlots: "2", pairSelectionMode: "auto", maxLeverage: "5", acks: { high: true } });
  assert.deepEqual(body, { risk_pct: 1, acknowledge_high_risk: true, pair: "ETH/USDT", pair_selection_mode: "auto", allocated_capital: 50, daily_loss_limit_usd: 9, live_slots: 2, max_leverage: 5 });
});

test("bad or empty numbers are left out instead of sent", () => {
  const body = buildRequest({ ...form, riskPct: "abc", capitalInput: "", limitInput: "-3" });
  assert.deepEqual(body, {});
});

test("slots are sent only when the service offers more than one, leverage only on Futures", () => {
  assert.equal("live_slots" in buildRequest({ ...form, account: { ...account, live_multi_available: false }, liveSlots: "3" }), false);
  assert.equal("max_leverage" in buildRequest({ ...form, account: { ...account, market_type: "spot" }, marketType: "spot", maxLeverage: "5" }), false);
});

test("the same form gives the same key, a changed one a different key", () => {
  assert.equal(requestKey(buildRequest(form)), requestKey(buildRequest({ ...form })));
  assert.notEqual(requestKey(buildRequest(form)), requestKey(buildRequest({ ...form, riskPct: "2" })));
});

const notes = [
  { id: "a", severity: "tip", tab: "risk", text: "t" },
  { id: "b", severity: "warning", tab: "trading", text: "w" },
  { id: "c", severity: "not_possible", tab: "risk", text: "n" },
  { id: "d", severity: "warning", tab: "risk", text: "w2" },
  { id: "e", severity: "warning", tab: "risk", text: "w3" },
  { id: "f", severity: "warning", tab: "risk", text: "w4" },
];

test("notes are ordered not possible, warning, tip and keep the server order inside a group", () => {
  assert.deepEqual(sortNotes(notes).map((n) => n.id), ["c", "b", "d", "e", "f", "a"]);
});

test("each tab shows its own notes", () => {
  assert.deepEqual(notesForTab(notes, "trading").map((n) => n.id), ["b"]);
  assert.deepEqual(notesForTab(notes, "risk").map((n) => n.id), ["c", "d", "e", "f", "a"]);
});

test("the dashboard shows no tips and at most three notes", () => {
  const shown = dashboardNotes(notes);
  assert.equal(shown.length, MAX_DASHBOARD_NOTES);
  assert.ok(shown.every((n) => n.severity !== "tip"));
  assert.equal(shown[0].id, "c");
  assert.deepEqual(dashboardNotes([{ id: "x", severity: "tip", tab: "risk", text: "t" }]), []);
  assert.deepEqual(dashboardNotes(null), []);
});
