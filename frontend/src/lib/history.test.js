import assert from "node:assert/strict";
import test from "node:test";

import { filterCounts, historyRows, pageOf, PAGE_SIZE } from "./history.js";

const t = (id, pnl) => ({ id, pnl, ms: id });
const closed = [t(1, 5), t(2, -3), t(3, 0), t(4, 8), t(5, -1)]; // oldest first

test("newest first, and the filters pick wins or losses only", () => {
  assert.deepEqual(historyRows(closed).map((x) => x.id), [5, 4, 3, 2, 1]);
  assert.deepEqual(historyRows(closed, "wins").map((x) => x.id), [4, 1]);
  assert.deepEqual(historyRows(closed, "losses").map((x) => x.id), [5, 2]);
});

test("a break-even trade shows under All only; counts add up", () => {
  const c = filterCounts(closed);
  assert.deepEqual(c, { all: 5, wins: 2, losses: 2 });
  assert.ok(historyRows(closed, "wins").every((x) => x.pnl > 0));
  assert.ok(!historyRows(closed, "losses").some((x) => x.pnl === 0));
});

test("paging: first page, show more, nothing more at the end", () => {
  const many = Array.from({ length: PAGE_SIZE * 2 + 3 }, (_, i) => t(i, 1));
  const p1 = pageOf(historyRows(many), 1);
  assert.equal(p1.shown.length, PAGE_SIZE);
  assert.equal(p1.hasMore, true);
  const p3 = pageOf(historyRows(many), 3);
  assert.equal(p3.shown.length, PAGE_SIZE * 2 + 3);
  assert.equal(p3.hasMore, false);
  assert.deepEqual(pageOf([], 1), { shown: [], hasMore: false });
  assert.equal(pageOf(many.slice(0, PAGE_SIZE), 1).hasMore, false); // exactly one full page: no "show more"
});
