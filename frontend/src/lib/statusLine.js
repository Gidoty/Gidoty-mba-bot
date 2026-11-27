// The status line above the bot card (the server side is src/mba_bot/status_line.py, off unless STATUS_LINE is on). Pure functions only.
// Run with: node --test frontend/src/lib/statusLine.test.js (also run by tests/test_frontend_status_line.py)
export const TONE = { running: "ok", paused: "mute", waiting: "mute", on_hold: "warn", delayed: "warn", problem: "bad" };

// What the line shows for the server's answer, or null when there is nothing to show (the switch is off, the request failed, or the answer is not the shape the server sends).
export function viewOf(data) {
  if (!data || typeof data.headline !== "string" || typeof data.state !== "string") return null;
  return {
    tone: TONE[data.state] ?? "mute",
    headline: data.headline,
    detail: typeof data.detail === "string" ? data.detail : null,
    last: typeof data.last_check === "string" ? `Last check: ${data.last_check}` : "Not checked yet",
  };
}
