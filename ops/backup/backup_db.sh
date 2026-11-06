#!/bin/bash
# Nightly database backup, extended for off-server copies. Repository copy of /opt/mba-bot/backup_db.sh.
#
# UNCHANGED from the script that has run on the server since at least 5 Nov (as described by the owner; the server's file is
# the source of truth, so DIFF IT against this one before installing): sqlite3 .backup into BACKUP_DIR, PRAGMA integrity_check
# on the copy, and the 30-day local delete of mba_bot-*.db. The paths and the 30 days are the same defaults as before.
#
# ADDED:
#   * defaults for the paths can be overridden by MBA_* variables (used by the tests; cron sets none, so nothing changes);
#   * a trap that sends ONE alert naming the failing step when anything fails (before: silent). It never changes the exit status;
#   * two success markers under STATE_DIR (epoch seconds) that the watchdog reads;
#   * a last step that hands the new, integrity-checked copy to mba_offsite.py (encrypt, upload, verify). That step reads
#     ONLY the one file named on its command line. It never lists or globs BACKUP_DIR, so env.pre-* files cannot be sent, and it
#     deletes nothing, so this script stays the only pruner of BACKUP_DIR.
# The local stage finishes (backup, check, marker, prune) BEFORE the off-server step, so a failed upload never affects it.
set -e

DB="${MBA_DB:-/opt/mba-bot/mba_bot.db}"
BACKUP_DIR="${MBA_BACKUP_DIR:-/opt/mba-bot/backups}"
STATE_DIR="${MBA_BACKUP_STATE_DIR:-/var/lib/mba-backup}"
OPS_DIR="${MBA_OPS_DIR:-/opt/mba-bot/ops/backup}"
PYTHON3="${MBA_PYTHON3:-/usr/bin/python3}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
STEP=start
ALERTED=0

on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$ALERTED" -eq 0 ]; then
    "$PYTHON3" "$OPS_DIR/mba_alert.py" backup_failed "$STEP" || true
  fi
  exit "$rc"
}
trap on_exit EXIT

mark_local_success() {
  mkdir -p "$STATE_DIR" && date -u +%s > "$STATE_DIR/last_local_success.tmp" && mv -f "$STATE_DIR/last_local_success.tmp" "$STATE_DIR/last_local_success"
}

STEP=local_backup
mkdir -p "$BACKUP_DIR"
sqlite3 "$DB" ".backup '$BACKUP_DIR/mba_bot-$TIMESTAMP.db'"
STEP=integrity_check
sqlite3 "$BACKUP_DIR/mba_bot-$TIMESTAMP.db" "PRAGMA integrity_check;" | grep -qx "ok"
STEP=marker
mark_local_success || true
STEP=local_prune
find "$BACKUP_DIR" -type f -name "mba_bot-*.db" -mtime +30 -delete

STEP=offsite
"$PYTHON3" "$OPS_DIR/mba_offsite.py" "$BACKUP_DIR/mba_bot-$TIMESTAMP.db" || { rc=$?; if [ "$rc" -eq 3 ]; then ALERTED=1; fi; exit "$rc"; }
