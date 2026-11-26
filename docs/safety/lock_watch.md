# Lock watch (observation only)

Production runs SQLite with a 15 second busy timeout. When a write waits that long, the line in the log says only that the database was locked, not who held it. This module records who held it.

## What it does

- Hooks the SQLite engine and times every WRITE transaction, from its first INSERT, UPDATE, DELETE or REPLACE to its commit or rollback. A transaction that only reads is not timed.
- When one lasts more than `LOCK_WATCH_SECONDS` (default 3), it logs one WARNING line: the duration, how it ended, and the names of the functions of this package that were running when the transaction started (innermost first, at most six, joined with `<-`). No statement text, no parameters, no ids, no file paths, no secrets.
- At most 20 lines per hour.
- `LOCK_WATCH_SECONDS=0` (or a negative number) switches it off. An unreadable value falls back to 3, so a typo never stops the service from starting.
- At start-up, one INFO line per start-up step (`run_startup_default`, `recover_once`): the wall time of the step, the number of write transactions it made, the total time they held the database and the longest single hold.

Example line: `write transaction held the database for 4.2s (ended by commit; started in reconcile_trade<-reconcile_account<-run_recovery_forever)`.

## Why it is on by default

It changes no statement, no transaction, no timeout and no lock. Every hook body is wrapped so that it cannot raise into the database layer; tests make the clock, the call-site walk, the log call and the counters each fail and show the writes still work. The cost is one string check per statement and one frame walk per write transaction: measured in a scratch benchmark at below the noise of a durable commit (about 1 ms per transaction on this machine, extra cost not measurable above the run-to-run spread of about 50 microseconds).

## Limits

- It names who STARTED a long write transaction, not who was waiting for the lock. The victim of a lock (the writer that timed out) is the one the existing error line already shows.
- The clock starts when the first write statement is issued, before SQLite grants the lock, so the measured time can include the time that statement waited for the lock. Read a line as "this write transaction was open for N seconds".
- Hooks are attached to SQLite engines only.
