import test from "node:test";
import assert from "node:assert/strict";
import { ACTIONS, buildRequest, settingBadge, settingProblem } from "./adminActions.js";

const ok = { reason: "support ticket 123", code: "123456", target: "abc-123" };

test("every action builds the route the server has", () => {
  assert.equal(buildRequest("pause", ok).path, "/admin/customers/abc-123/pause");
  assert.equal(buildRequest("extend", { ...ok, days: 30 }).path, "/admin/customers/abc-123/subscription/extend");
  assert.equal(buildRequest("approve", ok).path, "/admin/requests/abc-123/approve");
  assert.equal(buildRequest("kill_engage", { reason: ok.reason, code: ok.code }).path, "/admin/kill-switch/engage");
  assert.equal(ACTIONS.length, 12);
});

test("a reason and a code are always needed", () => {
  assert.ok(buildRequest("pause", { ...ok, reason: "no" }).error);
  assert.ok(buildRequest("pause", { ...ok, code: "" }).error);
  assert.ok(buildRequest("nope", ok).error);
});

test("the target can never change the path", () => {
  for (const bad of ["../../settings", "a/b", "a b", "", "x".repeat(41), "a?b=c"]) assert.ok(buildRequest("pause", { ...ok, target: bad }).error, bad);
});

test("days and note are bounded", () => {
  for (const d of [0, 367, 1.5, "x"]) assert.ok(buildRequest("extend", { ...ok, days: d }).error);
  assert.equal(buildRequest("extend", { ...ok, days: 366 }).body.days, 366);
  assert.ok(buildRequest("note", { ...ok, note: "" }).error);
  assert.ok(buildRequest("note", { ...ok, note: "x".repeat(501) }).error);
  assert.equal(buildRequest("note", { ...ok, note: " hello " }).body.note, "hello");
});

test("setting boxes check type and bounds", () => {
  const num = { type: "int", min: 60, max: 3600 };
  assert.equal(settingProblem(num, "300"), null);
  for (const v of ["59", "3601", "1.5", "", "abc"]) assert.ok(settingProblem(num, v), v);
  assert.equal(settingProblem({ type: "bool" }, "On"), null);
  assert.ok(settingProblem({ type: "bool" }, "maybe"));
  assert.equal(settingProblem({ type: "choice", choices: ["open", "closed"] }, "Closed"), null);
  assert.ok(settingProblem({ type: "choice", choices: ["open", "closed"] }, "x"));
  assert.ok(settingProblem(undefined, "1"));
});

test("the badge says where the value comes from and whether a restart is needed", () => {
  assert.match(settingBadge({ source: "database", restart_required: true, customer_visible: false }), /set in the dashboard, needs a restart$/);
  assert.match(settingBadge({ source: "environment", restart_required: false, customer_visible: true }), /from the environment, takes effect without a restart, customers can see it/);
});

test("completing a deletion needs the exact confirmation and goes to its own route", () => {
  assert.ok(buildRequest("complete_deletion", ok).error);
  for (const bad of ["delete 12345678", "DELETE 1234", "DELETE ../etc/x", "DELETE 123456789", ""]) assert.ok(buildRequest("complete_deletion", { ...ok, confirm: bad }).error, bad);
  const r = buildRequest("complete_deletion", { ...ok, confirm: "DELETE 1a2b3c4d" });
  assert.equal(r.path, "/admin/requests/abc-123/complete-deletion");
  assert.equal(r.body.confirm, "DELETE 1a2b3c4d");
});
