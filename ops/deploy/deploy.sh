#!/bin/bash
# One safe deploy of the main branch to this server. Owner-run, on the server. Refuses when a live trade is open.
#   ops/deploy/deploy.sh --expect-head <sha> [--dry-run]
# Order: live-trade gate, preflight (expected head, fast-forward only, clean tree), gate again, stop the service, backup and integrity check, fast-forward merge, install requirements only if they changed, import check,
# alembic only if a migration file arrives, start, post-checks (service active, health 200, first scheduler round, no traceback). If anything fails after the stop, the code is put back to the old commit and the
# service is started again (the database is NOT restored automatically: migrations here are additive, and the pre-deploy copy is named in the message). Prints no secret.
set -Eeuo pipefail
. "$(dirname "$0")/common.sh"
here="$(cd "$(dirname "$0")" && pwd)"
expect=""; dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-head) expect="${2:-}"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -n "$expect" ] || die "usage: deploy.sh --expect-head <sha> [--dry-run]"

live_trade_gate || exit 1
info="$("$here/preflight.sh" "$expect")" || exit 1
old="$(sed -n 's/^OLD=//p' <<<"$info")"; new="$(sed -n 's/^NEW=//p' <<<"$info")"
migration="$(sed -n 's/^MIGRATION=//p' <<<"$info")"; deps="$(sed -n 's/^DEPS=//p' <<<"$info")"
say "$info"
if [ "$old" = "$new" ]; then say "Already at ${new:0:12}: nothing to deploy, the service was not touched."; exit 0; fi
if [ "$dry" = 1 ]; then
  say "DRY RUN: would stop $SERVICE, back up $DB_PATH, fast-forward ${old:0:12} to ${new:0:12}$([ "$deps" = yes ] && echo ', install requirements')$([ "$migration" = yes ] && echo ', run alembic upgrade heads'), start and check. Nothing was changed."
  exit 0
fi

live_trade_gate || exit 1   # again, just before the stop
stopped=0; backup_line=""
rollback() {
  local rc=$?
  trap - ERR
  if [ "$stopped" = 1 ]; then
    say "FAILED (exit $rc). Putting the code back to ${old:0:12} and starting the service again."
    git_app reset --hard --quiet "$old" || true
    "$SYSTEMCTL" start "$SERVICE" || true
    say "The database was NOT restored. ${backup_line:-No backup was made yet.}"
  fi
  exit 1
}
trap rollback ERR

"$SYSTEMCTL" stop "$SERVICE"; stopped=1
if "$SYSTEMCTL" is-active --quiet "$SERVICE"; then false; fi
backup_line="$("$here/backup_check.sh" predeploy)"; say "$backup_line"
git_app merge --ff-only --quiet "$new"
[ "$(git_app rev-parse HEAD)" = "$new" ]
if [ "$deps" = yes ]; then say "requirements.txt changed: installing"; (cd "$APP_DIR" && "$PYTHON" -m pip install --quiet -r requirements.txt); fi
if ! (cd "$APP_DIR" && PYTHONPATH=src DATABASE_URL="sqlite:///:memory:" "$PYTHON" -c "import mba_bot.api.app"); then say "import check FAILED"; false; fi
say "import check ok"
if [ "$migration" = yes ]; then
  say "a migration file arrived: alembic upgrade heads"
  if ! (cd "$APP_DIR" && DATABASE_URL="sqlite:///$DB_PATH" $ALEMBIC upgrade heads); then say "alembic upgrade FAILED"; false; fi
  (cd "$APP_DIR" && DATABASE_URL="sqlite:///$DB_PATH" $ALEMBIC current) || true
else
  say "no migration file: alembic not run"
fi
started_at="$(date -u '+%Y-%m-%d %H:%M:%S')"
"$SYSTEMCTL" start "$SERVICE"
wait_for_health || { say "the service did not become healthy in ${START_TIMEOUT}s"; false; }
say "service active, health 200"
if [ "$HEARTBEAT_WAIT" -gt 0 ]; then
  waited=0; beats=0
  while [ "$waited" -lt "$HEARTBEAT_WAIT" ]; do
    beats="$("$JOURNALCTL" -u "$SERVICE" --since "$started_at" --no-pager 2>/dev/null | grep -c 'round complete' || true)"
    [ "$beats" -ge 1 ] && break
    sleep 5; waited=$((waited + 5))
  done
  [ "$beats" -ge 1 ] || { say "no scheduler round was logged within ${HEARTBEAT_WAIT}s"; false; }
  say "first scheduler round logged"
fi
bad="$("$JOURNALCTL" -u "$SERVICE" --since "$started_at" --no-pager 2>/dev/null | grep -c -E 'Traceback|CRITICAL' || true)"
[ "$bad" -eq 0 ] || { say "the log has $bad Traceback or CRITICAL line(s) since the start"; false; }
trap - ERR
say "DEPLOYED ${old:0:12} -> ${new:0:12}"
say "ROLLBACK: $here/rollback.sh ${old:0:12}   (add --restore-db <file> only if the database must go back too; the pre-deploy copy is ${backup_line#backup ok: })"
