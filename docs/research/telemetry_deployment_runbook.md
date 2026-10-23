# Telemetry deployment runbook (HELD: not authorised, not run)

Status: prepared for review. **Nothing in this runbook has been executed.** Deployment waits for the project owner's explicit authorisation
after reading it. Production behaviour today is unchanged.

## What this turns on

`TRADE_TELEMETRY=1` makes the service write two kinds of rows next to what it already does: one `trade_telemetry` row per opened trade (signal,
indicator and sizing context) and bounded `decision_log` rows (a crossover seen, an entry attempted, an Auto scan result). It is observation only:

* nothing in the trading path reads those rows;
* telemetry functions never raise into the caller, never flush or commit (rows ride the commit the trading code already makes), and take no
  SQLite write lock of their own;
* no sizing, signal, order, stop, lifecycle or customer-facing behaviour changes; with the flag off, no telemetry code runs;
* it stores numbers and short machine codes only: no keys, tokens, customer text or exchange error text;
* the decision log is capped per account per day, so it cannot grow without bound.

A test runs the same cycle with the flag off and on and asserts the opened trade is identical. The telemetry code ships with the application but does nothing while the flag is off; the tables exist only after the migration below.

## What changes on the server

1. `alembic upgrade head` creates two empty tables (`trade_telemetry`, `decision_log`), revision `d4a8f1c3e6b2`. No existing table or column is altered.
2. One environment variable, `TRADE_TELEMETRY=1`, in the service environment.
3. One service restart (the bot restarts its scheduler; open positions keep their exchange-native stops throughout, as for any restart).

## Procedure (the owner runs it, or authorises me to give exact commands for the VPS)

Pre-checks (read-only):

```bash
cd /opt/mba-bot
git rev-parse HEAD                                    # record the deployed commit
df -h /opt/mba-bot ; free -h                          # room for a database copy and a restart
python -m mba_bot.research telemetry-check --database /opt/mba-bot/mba_bot.db   # shows current revision, expects no telemetry rows yet
```

Quiet moment: pick a time with no open position if you can (`open_trades` in the check output). A restart is safe with an open position,
but a quiet moment removes the question.

1. Back up the database (a copy; the file is not modified): `sqlite3 /opt/mba-bot/mba_bot.db ".backup /opt/mba-bot/backup/mba_bot.pre-telemetry.db"`.
2. Run the migration: `cd /opt/mba-bot && alembic upgrade head`. Expected: one revision applied, `d4a8f1c3e6b2`. If it fails, stop; nothing else has changed.
3. Add `TRADE_TELEMETRY=1` to the service environment file. Do not change any other setting.
4. `sudo systemctl restart mba-bot`, then `sudo systemctl status mba-bot` and `journalctl -u mba-bot -n 100 --no-pager`. Expected: normal start, no errors.
5. Verify: run `telemetry-check` again immediately, then after the next real signal or opened trade, and after 24 hours.

## Acceptance checks

* The service is active and the scheduler cycles on its usual cadence; no new error category in the journal.
* `telemetry-check` shows `revision_is_telemetry_head: True`, the tables exist, `decision_rows` grows slowly and stays within the per-account daily cap.
* After the next opened trade: `telemetry_rows` increased by one and `trades_without_telemetry_since_first_row` is 0.
* Trade behaviour compared with the same period before: same sizing rule, same stops, same exits. Customers notice nothing.

## Rollback (any time, in this order)

1. Remove or set `TRADE_TELEMETRY=0`, restart the service. Telemetry stops at once; nothing else changes; existing rows are inert.
2. The two tables can stay. Dropping them is optional and is `alembic downgrade c7d2e5f8a1b3` (only after step 1). Restoring the backup is not needed for a rollback because no existing table was altered.

## Risks, stated plainly

* Extra rows in the same commit slightly enlarge each commit. The tables are small (a few rows per trade, bounded decision rows).
* A bug in telemetry code is designed to be swallowed (logged by exception type only). If it ever shows up in the journal, turn the flag off.
* The first restart briefly pauses scheduling, the same as every previous deployment.

## What I need from you before this runs

Written authorisation to deploy, the time window, and whether you want to run the commands yourself or have me prepare a single script to
review. No production change is made until then.
