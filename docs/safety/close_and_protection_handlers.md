# Failure handlers on the close, stop-move and protection paths

When a database write fails and the session rolls back, the account and trade objects are expired: the next read of any attribute is a database load, and it fails again if the database is still locked. A failure handler that reads such an attribute (for example the account id for a log line) then raises instead of doing its job.

## What changed

Failure handling only. No order, price, quantity, strategy, entry, exit or stop decision changed, and the order of exchange actions is the same. No exchange call was added. No default, timeout or switch changed. No migration.

- Handlers read nothing from the database. The values they need (account id, pair, trade id, alert text) are read before the `try` that can fail, through helpers that cannot raise (`_safe_account_id`, `_safe_get`). Every log call in these handlers is guarded.
- Sites: `finalize_exchange_close` (cleanup before a close), `execute_exit` (cancel not confirmed, exchange state not readable, exit order attempt failed), `establish_protection` (protective-stop attempt failed), `_resolve_spot_quantity`, `_resolve_entry_submission_error`, `move_stop` (failed trailing move: the cancel of a possibly placed new stop and the customer message), `_flag_withdrawal_block` in trader.py.
- The job of each handler still happens: the deferred return, the emergency retry, the next protection attempt, the cancel and the message, the continuation after a failed cleanup.
- The close is recorded when the trade object is expired and the database is briefly locked. `_finalize` reloads the trade first. The write that records the close is retried locally, at most two more times, 0.25 s apart, and only for "database is locked". No exchange call is repeated.
- `_notify` reads the customer's target inside its guard.
- `_flag_withdrawal_block` now never raises. If its commit fails it logs, rolls back only if the session cannot continue, sends no alert (the alert follows a durable record, as before) and returns; the next round tries again. This round's entry block does not depend on the stored flag.

## Left unchanged on purpose

- If the lock is still there after the retries, `_finalize` raises as before; the trade stays in its state and the reconciler records the close on a later pass.
- The sites that only affect new entries, scans and reports, and the handlers after a close or stop move is already recorded, are not touched.
- `_cas` and `_set` still roll back and raise on a failed commit. Only the close record is retried.

## Tests

`tests/test_close_and_protection_handlers.py`: one test per site with the account unreadable at the moment the handler runs, a real-database close with an expired trade and a brief lock, a close whose own commit is briefly locked, and the limits of the retry (only a locked database, at most two retries, short pause, nothing retried against the exchange).
