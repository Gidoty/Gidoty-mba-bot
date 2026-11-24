import test from "node:test";
import assert from "node:assert/strict";
import { emailProblem, emailStatusText, isRecoveryRoute, newPasswordProblem, tokenFromRoute } from "./recovery.js";

const TOKEN = "A".repeat(43);

test("tokens are read only from the two screens and only in the shape the server issues", () => {
  assert.equal(tokenFromRoute(`reset-password/${TOKEN}`, "reset-password"), TOKEN);
  assert.equal(tokenFromRoute(`verify-email/${TOKEN}`, "verify-email"), TOKEN);
  assert.equal(tokenFromRoute(`verify-email/${TOKEN}`, "reset-password"), null);
  for (const bad of ["reset-password/", "reset-password/short", `reset-password/${TOKEN}/x`, `reset-password/${TOKEN}?a=1`, `reset-password/${"A".repeat(101)}`]) assert.equal(tokenFromRoute(bad, "reset-password"), null, bad);
  assert.equal(tokenFromRoute(undefined, "reset-password"), null);
});

test("recovery routes are recognised", () => {
  assert.equal(isRecoveryRoute("forgot-password"), true);
  assert.equal(isRecoveryRoute(`reset-password/${TOKEN}`), true);
  assert.equal(isRecoveryRoute("login"), false);
  assert.equal(isRecoveryRoute(undefined), false);
});

test("password and email checks", () => {
  assert.equal(newPasswordProblem("long-enough-1", "long-enough-1"), null);
  assert.ok(newPasswordProblem("short", "short"));
  assert.ok(newPasswordProblem("x".repeat(73), "x".repeat(73)));
  assert.ok(newPasswordProblem("long-enough-1", "different-1234"));
  assert.equal(emailProblem("a@b.co"), null);
  for (const bad of ["", "a", "a@b", "a b@c.de"]) assert.ok(emailProblem(bad), bad);
});

test("the account screen says plainly when email is not set up", () => {
  assert.match(emailStatusText({ available: false }), /cannot send email yet/);
  assert.match(emailStatusText({ available: true, verified: true }), /confirmed\.$/);
  assert.match(emailStatusText({ available: true, verified: false }), /not confirmed/);
  assert.match(emailStatusText(null), /Checking/);
});
