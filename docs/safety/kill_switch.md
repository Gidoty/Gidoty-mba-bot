# Operator kill switch

Status: on a branch, not merged, not deployed. Design approved by the owner on 4 Nov 2026 (decisions D1 to D7 as recommended, plus two changes: reads retry before failing closed, and `engage` prints the exact release command).

## What it does
* **Engaged:** no new LIVE entry on any account. Paper entries continue.
* **Open positions are NOT closed.** Stop monitoring, protection, reconciliation, strategy exits, emergency exits and recovery all keep running. Protection is never gated by the switch.
* **Default: released.** Shipping this changes nothing until the owner engages it.

## Where the state lives
Two sources, both read fresh on every check (no cache). Engaged if EITHER says engaged.
1. **Database:** table `kill_switch` (one row, id 1: `engaged`, `reason`, `changed_at`, `changed_by`) and an append-only audit table `kill_switch_events` (one row per change: time, action `engage`/`release`/`seed`, actor, reason, previous state).
2. **Break-glass file:** `KILL_SWITCH_FILE` (default: `KILL_SWITCH` next to the lock file). If it exists, the switch is engaged whatever the database says. Use it when the database is locked or the command fails: `touch <file>` engages, `rm <file>` removes it.

### Fail closed, with retries
The answer is ENGAGED if the table cannot be read, the row is missing or malformed, or the file check fails with anything other than "not found". A failed read is first retried for `KILL_SWITCH_READ_RETRY_SECONDS` (default 3, every `KILL_SWITCH_READ_RETRY_INTERVAL_SECONDS`, default 0.5), because a locked database is usually momentary. Every answer carries the path taken and the number of attempts, and the log says so:

| path | meaning |
|---|---|
| `released` | database released, file absent |
| `engaged_db`, `engaged_file`, `engaged_db_and_file` | engaged by the database, the file, or both |
| `fail_closed_db_unreadable` | the table could not be read within the retry window |
| `fail_closed_row_missing` | no state row although audit events exist (anomaly) |
| `fail_closed_row_malformed` | `engaged` is not 0 or 1 |
| `fail_closed_file_error` | the file check failed for a reason other than "not found" |

The first run seeds a RELEASED row only when there is no row AND no audit event has ever been written (`init_db` and the migration both do this). A missing row with events present stays missing, so trading stays paused until the owner repairs it.

## Where it is checked (all before any order, none on a protection path)
1. `trader._maybe_enter`: first thing for a live entry, before any exchange call.
2. `trader._maybe_enter`: again right before `lifecycle.open_live_trade`, because the checks in between make exchange calls.
3. `lifecycle.open_live_trade`: before the write-ahead row and any order; raises `kill_switch.EntryPaused` (a subclass of `EntryForbidden`), so any other caller is covered too.
Plus a visibility line in `process_account`: while engaged, one warning per live account per `PROTECTION_ALERT_REPEAT_HOURS` names the path, who, since when and why.
A refusal logs a decision row (`kill_switch_engaged`, with `path`, `check`, `attempts`) when telemetry is on, does NOT set `last_error` (it is not a fault), and tells the customer at most once per repeat window, in fixed words that never contain the operator's reason.
Worst case after a flip: an entry whose first order is already in flight completes and is then protected.

## Operating it (the owner, over SSH, as the app user, FROM THE APP DIRECTORY)
The package is not installed in the server's virtual environment, so Python must be told where the code is. A bare `python -m mba_bot.killswitch` fails there with `No module named 'mba_bot'` (seen on the first deploy). Use this form, from `/opt/mba-bot` (the database path in `DATABASE_URL` is relative to the directory you run it from):
```
cd /opt/mba-bot
PYTHONPATH=/opt/mba-bot/src .venv/bin/python -m mba_bot.killswitch status
PYTHONPATH=/opt/mba-bot/src .venv/bin/python -m mba_bot.killswitch engage  --reason "why"
PYTHONPATH=/opt/mba-bot/src .venv/bin/python -m mba_bot.killswitch release --reason "why" --confirm RELEASE
```
* `engage` is one step. It prints the exact release and status commands for the interpreter and folder it ran from, ready to copy, plus the directory to run them in.
* `release` needs a reason and the typed word `RELEASE`; it warns if the break-glass file still exists.
* If the database write fails (for example a lock that does not clear), the command says FAILED, exits 1 and prints the `touch` command. It never reports success for a write that did not happen.
* If the SQLite file named by `DATABASE_URL` does not exist from where you ran it (usually the wrong directory), the command says so and stops, without creating an empty database.
* The command never creates tables, never prints secrets, and only the owner can run it. There is no web route: the app has no admin role.

## Restarts, deploys and rollback
* The state is in the database: a restart, reboot or deploy keeps it. Nothing clears it automatically.
* A database restore from backup restores the OLD flag: run `status` straight after any restore. The break-glass file survives a restore.
* **Rolling back to code that predates the switch (for example 9bf6272) ignores the table, so trading resumes.** Create the break-glass file first if it must stay stopped, and decide deliberately.
* `alembic downgrade` drops both tables (same warning).

## Rollout (the owner runs every step; nothing is applied by this PR)
1. Back up the database. Merge after the reviewer approves.
2. Deploy and restart. `init_db` creates the tables and the RELEASED row at startup; the Alembic migration `e5b9c2d7f1a4` (idempotent, additive, same result) can be applied afterwards or not at all. Applying it to production needs the owner's approval.
3. `status` (the command in the section above) shows RELEASED, path `released`.
4. Drill: `engage --reason "drill"`, wait one scheduler cycle (up to 5 minutes), look for the warning line per live account in the journal, then `release`. This proves the flag is read; the tests prove entries are refused.
5. Frontend: no change.

## Not covered
It does not close or reduce positions, cancel stops, stop the API (logins, billing, webhooks), or help if the process is wedged (`systemctl stop` is the harder stop, and it also stops stop monitoring and recovery). It is an operations control, not a defence against a compromised app process: the API and scheduler share one process and one OS user.

## Tests
`tests/test_kill_switch.py` (45 tests) with the fake exchange: it proves control flow, not Binance behaviour. Mutation report: see the pull request.
