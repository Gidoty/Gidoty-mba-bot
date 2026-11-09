// Run with: node --test frontend/src/lib   (also run by tests/test_frontend_lib.py)
import assert from "node:assert/strict";
import test from "node:test";

import { dayMonthYearTime, fmt, signed } from "./format.js";
import { closedTrades, cumulativeSeries, filterRange, dailyBuckets, isThin, monthlyBuckets, summarize, THIN_THRESHOLD } from "./stats.js";

const T = (iso, pnl, status = "closed") => ({ id: iso, status, closed_at: iso, pnl });
const trades = [
  T("2026-11-06T10:00:00Z", 50),
  T("2026-11-04T09:00:00Z", -20),
  T("2026-10-30T12:00:00Z", 30),
  T("2026-11-07T08:00:00Z", -40),
  { id: "open", status: "open", closed_at: null, pnl: null },
  { id: "nopnl", status: "closed", closed_at: "2026-11-01T00:00:00Z", pnl: null },
];

test("closedTrades keeps closed trades with a result, oldest first", () => {
  const c = closedTrades(trades);
  assert.deepEqual(c.map((t) => t.pnl), [30, -20, 50, -40]);
  assert.deepEqual(closedTrades(undefined), []);
});

test("summarize: win rate, averages, best, worst, profit factor, drawdown", () => {
  const s = summarize(closedTrades(trades));
  assert.equal(s.n, 4);
  assert.equal(s.wins, 2);
  assert.equal(s.losses, 2);
  assert.equal(s.totalPnl, 20);
  assert.equal(s.winRate, 50);
  assert.equal(s.avgWin, 40);
  assert.equal(s.avgLoss, -30);
  assert.equal(s.best.pnl, 50);
  assert.equal(s.worst.pnl, -40);
  assert.equal(s.profitFactor, 80 / 60);
  // cumulative: 30, 10, 60, 20 -> peak 60, trough after it 20
  assert.equal(s.maxDrawdown, 40);
});

test("summarize with no trades or no losses never divides by zero", () => {
  const empty = summarize([]);
  assert.equal(empty.n, 0);
  assert.equal(empty.winRate, null);
  assert.equal(empty.profitFactor, null);
  assert.equal(empty.best, null);
  assert.equal(empty.maxDrawdown, 0);
  const onlyWins = summarize(closedTrades([T("2026-11-01T00:00:00Z", 5), T("2026-11-02T00:00:00Z", 7)]));
  assert.equal(onlyWins.profitFactor, null);
  assert.equal(onlyWins.avgLoss, null);
});

test("a drawdown from the very first trade counts from a starting peak of 0", () => {
  const s = summarize(closedTrades([T("2026-11-01T00:00:00Z", -10), T("2026-11-02T00:00:00Z", -5)]));
  assert.equal(s.maxDrawdown, 15);
});

test("cumulativeSeries starts at 0 and adds each trade; range filter works", () => {
  const closed = closedTrades(trades);
  const all = cumulativeSeries(closed);
  assert.deepEqual(all.map((p) => p.v), [0, 30, 10, 60, 20]);
  const now = Date.parse("2026-11-08T00:00:00Z");
  const week = cumulativeSeries(closed, { days: 7, now });
  assert.deepEqual(week.map((p) => p.v), [0, -20, 30, -10]);
  assert.deepEqual(cumulativeSeries([]), []);
});

test("dailyBuckets covers every day, zero days included, UTC", () => {
  const closed = closedTrades(trades);
  const b = dailyBuckets(closed, { days: 14, endMs: Date.parse("2026-11-08T05:00:00Z") });
  assert.equal(b.length, 14);
  assert.equal(b[13].key, "2026-11-08");
  assert.equal(b.find((x) => x.key === "2026-11-06").value, 50);
  assert.equal(b.find((x) => x.key === "2026-11-05").value, 0);
  assert.equal(b.find((x) => x.key === "2026-11-05").count, 0);
  assert.equal(b.reduce((s, x) => s + x.value, 0), 20); // 30 (30 Oct) is inside the 14 days
});

test("monthlyBuckets groups by calendar month in order", () => {
  const m = monthlyBuckets(closedTrades(trades));
  assert.deepEqual(m.map((x) => [x.label, x.value, x.count]), [["Oct", 30, 1], ["Nov", -10, 3]]);
});

test("thin-sample threshold", () => {
  assert.equal(THIN_THRESHOLD, 9);
  assert.equal(isThin(0), true);
  assert.equal(isThin(8), true);
  assert.equal(isThin(9), false);
});

test("formatting: explicit signs, zero has none, missing is a dash", () => {
  assert.equal(signed(12.3), "+12.30");
  assert.equal(signed(-4), "-4.00");
  assert.equal(signed(0), "0.00");
  assert.equal(signed(-0.001), "0.00");
  assert.equal(signed(null), "-");
  assert.equal(fmt(10877.7), "10,877.70");
});

test("a trade exactly at midnight UTC belongs to one day only", () => {
  const closed = closedTrades([T("2026-11-07T00:00:00Z", 10), T("2026-11-06T23:59:59Z", 5)]);
  const b = dailyBuckets(closed, { days: 3, endMs: Date.parse("2026-11-08T12:00:00Z") });
  assert.equal(b.reduce((s, x) => s + x.count, 0), 2);
  assert.equal(b.find((x) => x.key === "2026-11-07").count, 1);
  assert.equal(b.find((x) => x.key === "2026-11-06").count, 1);
});

test("the same month in two different years stays two buckets, in date order", () => {
  const m = monthlyBuckets(closedTrades([T("2026-12-05T00:00:00Z", 3), T("2025-12-05T00:00:00Z", -1)]));
  assert.deepEqual(m.map((x) => [x.label, x.value]), [["Dec", -1], ["Dec", 3]]);
});

test("date with year, UTC", () => {
  assert.equal(dayMonthYearTime(Date.parse("2026-11-08T14:05:00Z")), "8 Nov 2026 14:05");
  assert.equal(dayMonthYearTime(Date.parse("2025-01-02T00:00:00Z")), "2 Jan 2025 00:00");
});

test("filterRange keeps trades inside the window, null keeps all, edge is inclusive", () => {
  const closed = closedTrades(trades);
  const now = Date.parse("2026-11-08T00:00:00Z");
  assert.deepEqual(filterRange(closed, 7, now).map((t) => t.pnl), [-20, 50, -40]);
  assert.equal(filterRange(closed, null, now).length, 4);
  assert.deepEqual(filterRange(closed, 0, now).length, 0);
  const edge = closedTrades([T("2026-11-01T00:00:00Z", 1)]);
  assert.equal(filterRange(edge, 7, now).length, 1); // exactly 7 days before now is inside
});
