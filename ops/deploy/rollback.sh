#!/bin/bash
# Put the code back to an earlier commit (one line), optionally the database too. Owner-run, on the server. Refuses when a live trade is open.
#   ops/deploy/rollback.sh <old-sha> [--restore-db <backup file>]
# The old commit must be an ancestor of the current HEAD. The database is restored only with --restore-db, after the file passes an integrity check; the current database is first copied aside, never overwritten.
set -euo pipefail
. "$(dirname "$0")/common.sh"
old="${1:-}"; shift || true
restore=""
while [ $# -gt 0 ]; do
  case "$1" in
    --restore-db) restore="${2:-}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ "$old" =~ ^[0-9a-f]{7,40}$ ]] || die "usage: rollback.sh <old-sha> [--restore-db <file>]"
git_app merge-base --is-ancestor "$old" HEAD || die "$old is not an ancestor of the current HEAD; nothing was changed"
if [ -n "$restore" ]; then
  [ -f "$restore" ] || die "the backup file does not exist"
  "$PYTHON" - "$restore" <<'PY' || die "the backup file fails its integrity check; nothing was changed"
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
sys.exit(0 if c.execute("PRAGMA integrity_check").fetchall() == [("ok",)] else 1)
PY
fi
live_trade_gate || exit 1
"$SYSTEMCTL" stop "$SERVICE"
if [ -n "$restore" ]; then
  aside="$DB_PATH.before-rollback-$(date -u +%Y%m%d-%H%M%S)"
  cp -p "$DB_PATH" "$aside"; rm -f "$DB_PATH-wal" "$DB_PATH-shm"
  cp -p "$restore" "$DB_PATH"
  say "database restored from the backup; the previous one is kept as $aside"
fi
git_app reset --hard --quiet "$old"
"$SYSTEMCTL" start "$SERVICE"
wait_for_health && say "ROLLED BACK to $(git_app rev-parse --short=12 HEAD); service active, health 200" || die "the service did not become healthy after the rollback"
