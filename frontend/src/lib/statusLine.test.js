import test from "node:test";
import assert from "node:assert/strict";
import { TONE, viewOf } from "./statusLine.js";

test("every state the server sends has a tone", () => {
  for (const s of ["running", "paused", "waiting", "on_hold", "delayed", "problem"]) assert.ok(TONE[s], s);
});

test("the view carries the server's words and a plain last-check line", () => {
  const v = viewOf({ state: "running", headline: "Running.", detail: "The bot checks the market every 5 minutes.", last_check: "2 minutes ago" });
  assert.deepEqual(v, { tone: "ok", headline: "Running.", detail: "The bot checks the market every 5 minutes.", last: "Last check: 2 minutes ago" });
  assert.equal(viewOf({ state: "delayed", headline: "x", detail: null, last_check: null }).last, "Not checked yet");
  assert.equal(viewOf({ state: "mystery", headline: "x" }).tone, "mute");
});

test("an off switch, an error or a wrong shape shows nothing", () => {
  for (const bad of [null, undefined, {}, { detail: "x" }, { state: 1, headline: "x" }, { state: "running" }]) assert.equal(viewOf(bad), null);
});
