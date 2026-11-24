#!/bin/bash
# Online backup of the SQLite database to BACKUP_DIR, then an integrity check of the COPY. Prints the file name, its size and its SHA-256 (no secret). Exit 0 only when the copy says "ok".
#   ops/deploy/backup_check.sh [label]        (label defaults to predeploy)
set -euo pipefail
. "$(dirname "$0")/common.sh"
label="${1:-predeploy}"
[[ "$label" =~ ^[A-Za-z0-9_-]{1,30}$ ]] || die "the label may only hold letters, digits, dash and underscore"
[ -f "$DB_PATH" ] || die "database file not found"
mkdir -p "$BACKUP_DIR"
dest="$BACKUP_DIR/mba_bot-$label-$(date -u +%Y%m%d-%H%M%S).db"
"$PYTHON" - "$DB_PATH" "$dest" <<'PY'
import os, sqlite3, sys
src, dest = sys.argv[1], sys.argv[2]
try:
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=20)
    b = sqlite3.connect(dest)
    a.backup(b)  # the online backup API: a consistent copy even if the file is in WAL mode
    verdict = b.execute("PRAGMA integrity_check").fetchall()
    b.close(); a.close()
except Exception as exc:  # a damaged page can stop the copy or the check itself: that is a failed check too
    print(f"backup FAILED: the database could not be copied or checked ({type(exc).__name__})", file=sys.stderr)
    sys.exit(1)
os.chmod(dest, 0o600)
if verdict != [("ok",)]:
    print("integrity check FAILED on the copy", file=sys.stderr)
    sys.exit(1)
PY
say "backup ok: $dest ($(wc -c < "$dest") bytes, sha256 $(sha256sum "$dest" | cut -d' ' -f1))"
