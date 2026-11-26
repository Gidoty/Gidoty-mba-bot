#!/bin/bash
# Shared settings and helpers for the owner-run deploy scripts (ops/deploy/README.md). Sourced, never run. It prints no secret: it never reads or prints the environment file, never runs `env` or `set -x`.
# Every path and command can be overridden by an MBA_* variable (the tests do; on the server nothing is set, so the defaults apply).
APP_DIR="${MBA_APP_DIR:-/opt/mba-bot}"
SERVICE="${MBA_SERVICE:-mba-bot}"
PYTHON="${MBA_PYTHON:-$APP_DIR/.venv/bin/python}"
DB_PATH="${MBA_DB:-$APP_DIR/mba_bot.db}"
BACKUP_DIR="${MBA_BACKUP_DIR:-$APP_DIR/backups}"
REMOTE="${MBA_REMOTE:-origin}"
BRANCH="${MBA_BRANCH:-main}"
HEALTH_URL="${MBA_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
SYSTEMCTL="${MBA_SYSTEMCTL:-systemctl}"
JOURNALCTL="${MBA_JOURNALCTL:-journalctl}"
CURL="${MBA_CURL:-curl}"
GIT="${MBA_GIT:-git}"
ALEMBIC="${MBA_ALEMBIC:-$PYTHON -m alembic}"
HEARTBEAT_WAIT="${MBA_HEARTBEAT_WAIT:-330}"   # seconds to wait for the first scheduler round after the start (a round is every 5 minutes); 0 skips the check
START_TIMEOUT="${MBA_START_TIMEOUT:-90}"

say() { printf '%s\n' "$*"; }
die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }
git_app() { "$GIT" -C "$APP_DIR" "$@"; }

# 0 only when no live trade is open (exit 3 = one is open, 2 = unreadable: both mean do NOT restart).
live_trade_gate() {
  local rc=0
  (cd "$APP_DIR" && PYTHONPATH=src DATABASE_URL="sqlite:///$DB_PATH" "$PYTHON" -m mba_bot.restart_gate) || rc=$?
  case "$rc" in
    0) return 0 ;;
    3) say "A live trade is open: do NOT restart." ; return 3 ;;
    *) say "The live-trade gate could not read the database (exit $rc): do NOT restart." ; return 2 ;;
  esac
}

wait_for_health() {
  local waited=0
  while [ "$waited" -lt "$START_TIMEOUT" ]; do
    if "$SYSTEMCTL" is-active --quiet "$SERVICE" && [ "$("$CURL" -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>/dev/null || true)" = "200" ]; then return 0; fi
    sleep 2; waited=$((waited + 2))
  done
  return 1
}
