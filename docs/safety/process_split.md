# Splitting the API and billing process from the trading worker: design, build, rollback

Status: BUILT, OFF. `PROCESS_ROLE` is unset by default, which means `all`: the single process serves HTTP and runs the scheduler exactly as today. Nothing changes until the owner installs two systemd units and starts them in place of the current one. **Depends on nothing** (based on main); it touches the same few lines as the operator-settings PR (`run_poll_forever` and `create_app`), so whichever merges second needs a small conflict resolution. **No migration, no new table.** Everything below that says "rehearsed" has NOT been rehearsed.

## 1. Why, in one paragraph
Today one process does everything (ARCHITECTURE.md): FastAPI, the Telegram and payment webhooks, billing jobs, and the trading scheduler. A change to a billing screen therefore means restarting the process that manages live trades, which the restart gate allows only when no live trade is open. After the split an API or billing release restarts only the API process; the worker keeps managing trades.

## 2. The two processes
| | `mba-api` (role `api`) | `mba-worker` (role `worker`) |
|---|---|---|
| Entry | `uvicorn mba_bot.api.app:app`, one worker, `PROCESS_ROLE=api` set in its unit | `python -m mba_bot.worker` |
| Serves HTTP | yes: customer API, admin dashboard, **Telegram webhook**, Paystack and NOWPayments webhooks | no |
| Trading rounds, recovery loop, stop-limit monitor, start-up reconciliation | **no** | yes |
| Billing jobs (period reminders, Paystack renewal recovery) | **yes** (`scheduler.run_billing_forever`, same jobs, same switches, same clocks) | no (`run_forever(billing=False)`) |
| Lock | its own file `MBA_API_LOCK_FILE` (taken when `API_SINGLE_INSTANCE_LOCK` is on) | the scheduler lock `MBA_BOT_LOCK_FILE`, the same one the single process takes |
| Daily-limit start-up default | no | yes (it is idempotent and belongs to the process that trades) |
| Needs | `SQLITE_WAL=1` | `SQLITE_WAL=1` |
`all` (default) takes the scheduler lock, so a single-process service cannot start while the worker runs, and two workers cannot run. The Telegram webhook only reads and writes the database and answers over HTTP, so it belongs in the API process; the worker sends Telegram messages outbound only.

## 3. How the two share the database
One SQLite file in WAL mode (`docs/security/wal_runbook.md`): readers do not block the writer and a writer does not block readers; writers still take turns, with `busy_timeout` (15 s by default). Each process has its own connection pool. The split roles REFUSE to start when `SQLITE_WAL` is off or when the file on disk is not in WAL mode (checked read-only), because a rollback journal would make two writers block each other. All coordination is in the database already: the kill switch (read fresh on every check), the protection alert marks (`alert_marks`), trade state and claims (atomic, deterministic client order ids), billing rows. The in-process state the API keeps (rate limiter, login throttle, the account screen's 30-second Binance-settings cache, the 60-second admin panel cache) is per process and was API-only already. Both units read the SAME environment file; **it must not contain `PROCESS_ROLE`** (a value there would apply to both: each process refuses a role that is not its own, so the mistake stops the wrong one instead of starting two schedulers or a trading loop inside the HTTP process).

## 4. Releasing code
* The worker imports every trading module when it starts (`process_roles.preload`, which also fails the start when a module cannot be imported), so a `git merge` on disk while it runs cannot be picked up half-way by a late import: it runs the code it started with until it is restarted. (This is also why a deploy script may update files before it knows which unit to restart.)
* **API or billing change only:** pull, run the import check, restart `mba-api`. A live trade being open does not matter: the worker keeps managing it. The live-trade gate is NOT required for an API-only restart, and `ops/deploy/deploy.sh` (separate PR) would need a `--only api` mode that skips the gate and the stop of the worker; it does not have one today, so use `systemctl restart mba-api` after `git merge --ff-only` and the import check.
* **Trading code change:** the same gate as today (`python -m mba_bot.restart_gate`, exit 0 only when no live trade is open), then restart `mba-worker`. The worker unit runs `python -m mba_bot.worker --check` before each start (role, WAL, lock, imports).
* **Additive migrations** still work with the old code running: the running process keeps working on the new schema until it is restarted; restart order after a migration: API first, then the worker at the next gate.
* Code that both processes import (models, config) is the one risk: a change to a model or a setting read by both should be released with both restarts, the worker at the gate. The migration rule (additive only) is what makes the gap safe.

## 5. What happens to a live trade at each restart
* **API restart:** nothing happens to the trade. The protective stop-loss is an order on the exchange and the worker keeps running the rounds, the recovery loop and the exits. Customers see a short error page; Telegram retries an undelivered webhook update, Paystack and NOWPayments retry theirs (their own retry rules, UNVERIFIED here).
* **Worker restart (gate open, no live trade):** the same as the single process today.
* **Worker restart or crash WITH a live trade (the gate refuses a planned one; a crash can still happen):** the stop-loss stays on the exchange and protects the position. Management stops for the gap: no crossover exit, no trailing move, no reconciliation of a stop that fired. On the next start the scheduler's own first step reconciles every live trade with the exchange before any round (`recover_once(include_healthy=True)`), exactly as after a crash of the single process today. `Restart=on-failure` brings the worker back in 10 seconds; `ops/health/mba_round_watchdog.py` (separate PR) is what tells the owner it was down.
* **Server reboot:** both units start; the worker reconciles first.

## 6. Switch, rollback and what stays the same
* Off by default: with `PROCESS_ROLE` unset every code path is the one that ran before (a test starts the app in `all` mode and checks the scheduler runs with its billing jobs, the lock is the scheduler lock and the start-up default runs).
* Units: `ops/systemd/mba-worker.service`, `mba-api.service` (templates, NOT installed), and `mba-bot.service.example`, the current single unit as the rollback target (compare it with the one actually installed before trusting it).
* **Cut over (owner, with no live trade open, after the rehearsals below):** back up the database; `python -m mba_bot.restart_gate`; stop the old unit; confirm `PRAGMA journal_mode` says `wal`; install and enable the two units; `systemctl start mba-worker` then `mba-api`; check `journalctl` for "round complete" from the worker and a 200 on `/api/health`; keep the old unit file but disabled.
* **Rollback:** `systemctl stop mba-api mba-worker` (through the gate if a live trade is open), `systemctl start mba-bot` (the old unit). Nothing in the database changed, so nothing to restore. One caveat: the daily-limit start-up default moves to the worker; the single process still runs it at its own start.

## 7. Risks, stated plainly
1. **Two writers on one SQLite file.** A long write in the API (a webhook handling, an admin action) can make a worker write wait up to `busy_timeout`; a worker round that holds a write transaction while it talks to the exchange would do the same to the API. The code was written for one process with several tasks; whether any path holds a write lock across an exchange call has been READ for, not measured. Rehearse under load.
2. **A hung worker looks alive to systemd** (the process is up, the rounds are not). That is what the round watchdog is for; the units have no `WatchdogSec` because the worker does not notify systemd.
3. **Config drift:** one environment file, edited by hand. A setting changed for the API but not applied to the worker (which needs a restart through the gate) behaves differently in each. The operator-settings table (separate PR) is read by both, but applied at different times.
4. **Operator settings cache:** per process, 5 seconds.
5. **A module imported by the worker but changed on disk** is not seen until restart, by design; a hot fix to trading code therefore needs the worker restart and the gate.
6. **The API still imports the trading modules** (routers read models and helpers), so the code on disk is shared; the split separates RUNNING processes, not code.
7. **`uvicorn --workers N`** is not supported: the billing jobs and the lock assume one API process.
8. Not changed: the in-process `rate_limit` and `login_throttle` state is per API process and is lost on an API restart (it already was).

## 8. What must be rehearsed before it is used (none of it has been)
On a COPY of the database, with a paper or demo account only (no real money), not on the production server:
1. WAL on: `PRAGMA journal_mode` is `wal`; start the worker, then the API; run `python -m mba_bot.worker --check` while the worker runs (it must refuse: the lock is held) and with a second API (must refuse).
2. A paper trade through its whole life with the API restarted in the middle; the trade closes normally and the log shows no lock errors.
3. `kill -9` the worker in the middle of a round with a paper trade open; start it; check the first round is preceded by the reconciliation and the trade state is unchanged or corrected.
4. Load: a burst of customer API calls and webhooks (a script posting signed test events) while rounds run; read the round time and any "database is locked" lines. Compare with the single process under the same load. Decide the acceptable round-time slip beforehand.
5. The Telegram link flow and a signed Paystack test event with the worker STOPPED (the API must answer them); then billing reminders and the renewal job: confirm each runs once, in the API process only, and not in the worker.
6. The cut-over and the rollback, end to end, on a throwaway server or a snapshot.
7. The restart-gate script and the round watchdog against the new unit names.

## 9. Not proven
Everything about systemd, the real service, real SQLite load, Nginx and Binance. The tests use temporary SQLite files, spies in place of the scheduler and a configuration parser for the unit templates.
