# Deploy scripts (owner-run, on the server)

Generalised from the deploy blocks used so far. **Nothing here is run by Claude and nothing here runs by itself.** They print no secret: they never read or print the environment file, never run `env` or `set -x`, and print only paths, commit ids, counts and one SHA-256 of a backup file. Settings come from `MBA_*` variables with the usual server paths as defaults (see `common.sh`: `MBA_APP_DIR=/opt/mba-bot`, `MBA_SERVICE=mba-bot`, `MBA_DB`, `MBA_BACKUP_DIR`, ...).

| Script | What it does |
|---|---|
| `deploy.sh --expect-head <sha> [--dry-run]` | The whole deploy, in this order: live-trade gate (refuses if a live trade is open or the database cannot be read), preflight, gate again, stop the service, backup and integrity check, fast-forward merge, `pip install` only if `requirements.txt` changed, import check, `alembic upgrade heads` only if a file arrived in `alembic/versions`, start, post-checks. |
| `preflight.sh <sha>` | Read-only. The remote branch is at the expected commit, the server's HEAD is an ancestor of it (fast-forward only), tracked files are clean. Prints OLD, NEW, MIGRATION, DEPS. |
| `backup_check.sh [label]` | Online backup (SQLite backup API, safe in WAL mode) to `MBA_BACKUP_DIR/mba_bot-<label>-<UTC time>.db`, mode 600, then `PRAGMA integrity_check` on the COPY. Exit 0 only on `ok`. |
| `rollback.sh <old-sha> [--restore-db <file>]` | Gate, stop, `git reset --hard <old>` (must be an ancestor of HEAD), start, health check. The database is restored only with `--restore-db`, after that file passes the integrity check, and the current file is kept as `mba_bot.db.before-rollback-<time>`. |

## What happens when something fails
Before the stop: nothing has changed; the script says what refused. After the stop, any failure (backup, merge, import, migration, health, a missing first scheduler round, a Traceback or CRITICAL line in the log) puts the code back to the old commit and starts the service again, then says so. **The database is never restored automatically**: the migrations in this repository are additive, the old code runs on the new schema, and a restore would throw away what customers did since the copy. The message names the pre-deploy copy; use `rollback.sh ... --restore-db` yourself if you decide you need it.

## Before the first use
1. Read the scripts: they are short. `bash -n` and the tests in `tests/test_ops_deploy.py` check syntax and behaviour against a fake service, a local git repository and a temporary database; they do not touch a real server (UNVERIFIED on the server: `systemctl`, `journalctl`, the real health URL).
2. Check the defaults at the top of `common.sh` match the server (service name, paths, `HEALTH_URL`). The heartbeat check looks for the text `round complete` in the service journal (the scheduler's own line); `MBA_HEARTBEAT_WAIT=0` skips it.
3. Run `deploy.sh --expect-head <sha> --dry-run` first: it runs the gate and the preflight and prints the plan without changing anything.
4. Several migrations from different branches can create more than one Alembic head: the script runs `upgrade heads`, and `preflight` does not hide that. Merge heads with `alembic merge` in a PR before deploying if you want a single line.

## Not covered
Off-server backups (`ops/backup/`, a separate runbook), Nginx and Cloudflare changes, environment-file edits (yours, by hand), and the rehearsal of a restart with a live trade open (the gate refuses; there is no override).
