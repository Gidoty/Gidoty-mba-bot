// Helpers for the password reset and email confirmation screens (the server side is src/mba_bot/api/routers/email_auth.py). Pure functions only. Run with: node --test frontend/src/lib/recovery.test.js
// (also run by tests/test_frontend_recovery.py). The token is in the URL fragment (#/reset-password/<token>), so it is never sent to a server as part of a page request.

export const MIN_PASSWORD = 8;
export const MAX_PASSWORD = 72;

// The token in a route like "reset-password/abc..." or "verify-email/abc...", or null when the route is not that screen or the token is not the shape the server issues.
export function tokenFromRoute(route, screen) {
  const prefix = `${screen}/`;
  if (typeof route !== "string" || !route.startsWith(prefix)) return null;
  const token = route.slice(prefix.length);
  return /^[A-Za-z0-9_-]{20,100}$/.test(token) ? token : null;
}

export function isRecoveryRoute(route) {
  return route === "forgot-password" || (typeof route === "string" && (route.startsWith("reset-password/") || route.startsWith("verify-email/")));
}

export function newPasswordProblem(password, again) {
  if (typeof password !== "string" || password.length < MIN_PASSWORD) return `Use at least ${MIN_PASSWORD} characters.`;
  if (password.length > MAX_PASSWORD) return `Use at most ${MAX_PASSWORD} characters.`;
  if (password !== again) return "The two passwords are not the same.";
  return null;
}

export function emailProblem(raw) {
  const v = String(raw ?? "").trim();
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v) ? null : "Enter the email address of your account.";
}

// What the Account screen says about email, from GET /api/auth/email/status.
export function emailStatusText(status) {
  if (!status) return "Checking email settings...";
  if (!status.available) return "This service cannot send email yet, so password reset and email confirmation are not available.";
  return status.verified ? "Your email address is confirmed." : "Your email address is not confirmed yet.";
}
