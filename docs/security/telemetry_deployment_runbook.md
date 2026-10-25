# Telemetry deployment runbook (branch `telemetry/deploy-prep`; run only after the PR is merged and you decide)

## What this adds

Two empty tables, `trade_telemetry` and `decision_log`, and code that fills them **only when `TRADE_TELEMETRY=1`**. It is observation only:

* nothing in the trading path reads those rows;
* the telemetry functions never raise into the caller. Decision rows are only queued in memory and are written by the next commit the trading code makes. Trade rows are written by two short synchronous helpers, **only when the flag is on**:
  * **paper entry:** the telemetry row is flushed inside a savepoint just before the trade's own commit, with no await in between;
  * **live entry:** after the live trade has been opened and committed, telemetry queues its row in a savepoint, flushes, and **commits once**. That commit also writes anything else pending in the same session at that moment, a little earlier than the trading code alone would have;
  * in both, a failing telemetry row rolls back only its own savepoint and the trade is unaffected, and the write lock is never held across an await;
* sizing, signals, orders, stops, lifecycle and customer-facing behaviour are unchanged; with the flag off, no telemetry code runs;
* it stores numbers and short machine codes only: no keys, tokens, customer text or exchange error text;
* the decision log is capped per account per day.

Why it matters: it records real fees, funding and fill context per trade, which replaces the assumed costs used in research.

## What this branch does NOT contain

The research package, Phase 2, the risk study and the fee-audit tool stay out of production. The migration is made **idempotent**: `init_db()` creates any missing table at every service start, so the tables will exist (empty) after the first restart of this code even with the flag off. The migration then does nothing instead of failing.

## Order (one step at a time; the service keeps running until step 6)

1. **Pre-checks (read-only).** `systemctl is-active mba-bot`; `sqlite3 'file:/opt/mba-bot/mba_bot.db?mode=ro' "select count(*) from trades where status not in ('closed','void');"` should be 0 for the restart in step 6.
   `sqlite3 'file:/opt/mba-bot/mba_bot.db?mode=ro' "select version_num from alembic_version;"` must print `c7d2e5f8a1b3`. Do not run `alembic stamp`.
2. **Back up.** `sqlite3 /opt/mba-bot/mba_bot.db ".backup '/opt/mba-bot/backups/pre-telemetry-$(date +%F-%H%M).db'"`, then `pragma integrity_check;` on the copy must print `ok`.
3. **Get the code (service still running the old code).** `cd /opt/mba-bot && git pull --ff-only`. `.venv/bin/pip install -r requirements.txt` (no new dependency expected).
4. **Check the chain.** `.venv/bin/alembic heads` must show exactly one head, `d4a8f1c3e6b2`. `.venv/bin/alembic current` must show `c7d2e5f8a1b3`. If either differs, stop.
5. **Run the migration** (additive: two new tables, no existing table touched; the old code is unaffected): `.venv/bin/alembic upgrade head`. Then `alembic current` must show `d4a8f1c3e6b2`, and `sqlite3 'file:/opt/mba-bot/mba_bot.db?mode=ro' ".tables"` lists `trade_telemetry` and `decision_log`.
6. **Restart with the flag still off.** `sudo systemctl restart mba-bot`. Check: `active`, one process, health 200, a scheduler round with `4 ok, 0 failed`, and `sudo journalctl -u mba-bot --since "<restart time>" | grep -ciE "traceback|critical|error"` is 0.
7. **Turn it on later, separately.** Add `TRADE_TELEMETRY=1` to the service environment (you edit `.env`; keep a copy first with `cp -a`), `sudo systemctl restart mba-bot`, and repeat the checks of step 6.

## Acceptance checks after step 7

* After the next real signal: `select count(*) from decision_log;` grows slowly and stays within the daily cap per account.
* After the next opened trade: `select count(*) from trade_telemetry;` rises by one, and `select count(*) from trades t where not exists (select 1 from trade_telemetry x where x.trade_id = t.id) and t.opened_at >= (select min(created_at) from trade_telemetry);` is 0.
* The trade itself is identical to before: same sizing rule, same stop, same exit. Customers notice nothing.

## Rollback (in this order)

1. Remove or set `TRADE_TELEMETRY=0`, restart. Telemetry stops at once; nothing else changes; existing rows are inert.
2. To remove the tables as well (optional): `alembic downgrade c7d2e5f8a1b3` (idempotent; only after step 1). Restoring the backup is not needed, because no existing table was altered.
3. Whole code rollback: `git checkout 478b0b2` and restart.

## Risks, stated plainly

* With the flag on, a live entry costs one extra short commit (described above). Watch the first real entry after step 7.
* Extra rows enlarge each commit slightly. The tables are small (a few rows per trade, bounded decision rows).
* A bug in telemetry is designed to be swallowed (logged by exception type only). If it ever appears in the journal, turn the flag off.
* A restart pauses scheduling briefly, as in every previous deployment; open positions keep their exchange-native stops.
