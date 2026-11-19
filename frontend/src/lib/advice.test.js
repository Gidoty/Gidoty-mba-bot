import assert from "node:assert/strict";
import test from "node:test";

import { MAX_DASHBOARD_NOTES, READ_MAX, READ_STORAGE_KEY, buildRequest, buttonState, createReadStore, dashboardNotes, groupNotes, noteKey, notesForTab, requestKey, sortNotes, textHash } from "./advice.js";

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

// ---------------------------------------------------------------------------------------------------- the pop-up: keys, groups, button, read state
const N = (id, severity, text = `text of ${id}`, tab = "risk") => ({ id, severity, text, tab });

test("a note key is its id plus a short hash of its text", () => {
  assert.match(noteKey(N("size_cap_ok", "tip", "Your limit is 3x.")), /^size_cap_ok:[0-9a-f]{8}$/);
  assert.equal(noteKey(N("a", "tip", "same")), noteKey(N("a", "warning", "same", "trading"))); // severity and tab are not part of it
  assert.notEqual(noteKey(N("a", "tip", "limit 3x")), noteKey(N("a", "tip", "limit 4x"))); // a changed number is a new note
  assert.notEqual(noteKey(N("a", "tip", "same")), noteKey(N("b", "tip", "same")));
  assert.equal(textHash("abc"), textHash("abc"));
  assert.equal(textHash("").length, 8);
});

test("the pop-up groups by severity in a fixed order and leaves empty groups out", () => {
  const groups = groupNotes([N("t1", "tip"), N("w1", "warning"), N("t2", "tip"), N("n1", "not_possible")]);
  assert.deepEqual(groups.map((g) => g.severity), ["not_possible", "warning", "tip"]);
  assert.deepEqual(groups.map((g) => g.label), ["Not possible", "Check", "Good to know"]);
  assert.deepEqual(groups[2].notes.map((n) => n.id), ["t1", "t2"]);
  assert.deepEqual(groupNotes([N("w1", "warning")]).map((g) => g.severity), ["warning"]);
  assert.deepEqual(groupNotes([]), []);
  assert.deepEqual(groupNotes(null), []);
});

test("the button shows while something is unread, with the unread count", () => {
  const notes = [N("a", "tip"), N("b", "warning")];
  assert.deepEqual(buttonState(notes, new Set()), { visible: true, count: 2, alert: false });
  assert.deepEqual(buttonState(notes, new Set([noteKey(notes[0])])), { visible: true, count: 1, alert: false });
  assert.deepEqual(buttonState(notes, new Set(notes.map(noteKey))), { visible: false, count: 0, alert: false });
  assert.deepEqual(buttonState([], new Set()), { visible: false, count: 0, alert: false });
  assert.deepEqual(buttonState(undefined, undefined), { visible: false, count: 0, alert: false });
});

test("a note whose words changed brings the button back", () => {
  const before = [N("size_cap_ok", "tip", "limit 3x")];
  const read = new Set(before.map(noteKey));
  assert.equal(buttonState(before, read).visible, false);
  assert.deepEqual(buttonState([N("size_cap_ok", "tip", "limit 4x")], read), { visible: true, count: 1, alert: false });
  assert.equal(buttonState([...before, N("new_one", "tip")], read).count, 1); // only the new one is counted
});

test("a not possible note keeps the button, with the alert, even when it has been read", () => {
  const notes = [N("risk_refused", "not_possible"), N("money_moves", "tip")];
  const all = new Set(notes.map(noteKey));
  assert.deepEqual(buttonState(notes, all), { visible: true, count: 1, alert: true }); // read, but it explains why Save will be refused
  assert.deepEqual(buttonState(notes, new Set()), { visible: true, count: 2, alert: true });
  assert.deepEqual(buttonState([N("money_moves", "tip")], all), { visible: false, count: 0, alert: false }); // fixed: the note is gone, so is the button
  assert.equal(buttonState([N("risk_refused", "not_possible", "different words")], all).count, 1);
});

function fakeStorage(initial = {}, { failGet = false, failSet = false } = {}) {
  const data = { ...initial };
  return {
    data,
    getItem(k) {
      if (failGet) throw new Error("blocked");
      return k in data ? data[k] : null;
    },
    setItem(k, v) {
      if (failSet) throw new Error("full");
      data[k] = String(v);
    },
  };
}

test("read state is kept in storage as note keys only", () => {
  const st = fakeStorage();
  const store = createReadStore(() => st);
  const key = noteKey(N("size_cap_ok", "tip", "Your limit is 3x."));
  store.markRead([key]);
  assert.equal(store.has(key), true);
  assert.deepEqual(JSON.parse(st.data[READ_STORAGE_KEY]), [key]);
  assert.ok(!st.data[READ_STORAGE_KEY].includes("limit")); // never the text
  const again = createReadStore(() => st); // a new visit reads it back
  assert.equal(again.has(key), true);
});

test("blocked storage still works for the visit, from memory", () => {
  for (const opts of [{ failGet: true }, { failSet: true }, { failGet: true, failSet: true }]) {
    const store = createReadStore(() => fakeStorage({}, opts));
    store.markRead(["a_note:0123abcd"]);
    assert.equal(store.has("a_note:0123abcd"), true);
  }
  const noStorage = createReadStore(() => undefined);
  noStorage.markRead(["a_note:0123abcd"]);
  assert.equal(noStorage.has("a_note:0123abcd"), true);
  const throwingGetter = createReadStore(() => {
    throw new Error("no storage");
  });
  throwingGetter.markRead(["a_note:0123abcd"]);
  assert.equal(throwingGetter.has("a_note:0123abcd"), true);
});

test("garbage in storage is ignored and only well-formed keys are kept", () => {
  assert.equal(createReadStore(() => fakeStorage({ [READ_STORAGE_KEY]: "not json" })).snapshot().size, 0);
  assert.equal(createReadStore(() => fakeStorage({ [READ_STORAGE_KEY]: '{"a":1}' })).snapshot().size, 0);
  const st = fakeStorage({ [READ_STORAGE_KEY]: JSON.stringify(["ok_one:0123abcd", "has spaces:0123abcd", "x:zzzzzzzz", 5, null, "<script>:00000000"]) });
  assert.deepEqual([...createReadStore(() => st).snapshot()], ["ok_one:0123abcd"]);
  const store = createReadStore(() => fakeStorage());
  store.markRead(["Bad Key", "text with words", 7, null, "fine_key:00ff00ff"]);
  assert.deepEqual([...store.snapshot()], ["fine_key:00ff00ff"]);
});

test("the list is capped and the oldest keys go first", () => {
  const store = createReadStore(() => fakeStorage());
  const keys = Array.from({ length: READ_MAX + 20 }, (_, i) => `n${i}:${String(i).padStart(8, "0")}`);
  store.markRead(keys);
  assert.equal(store.snapshot().size, READ_MAX);
  assert.equal(store.has(keys[0]), false);
  assert.equal(store.has(keys[keys.length - 1]), true);
});

test("subscribers hear about a change once, and not about a repeat", () => {
  const store = createReadStore(() => fakeStorage());
  let heard = 0;
  const off = store.subscribe(() => (heard += 1));
  store.markRead(["a:0123abcd"]);
  store.markRead(["a:0123abcd"]);
  assert.equal(heard, 1);
  off();
  store.markRead(["b:0123abcd"]);
  assert.equal(heard, 1);
});
