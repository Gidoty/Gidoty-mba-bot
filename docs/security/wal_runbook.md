# SQLite WAL and busy timeout (prepared, NOT deployed)

Why: with the default rollback journal, a writer blocks readers while it commits and every writer waits on one lock. WAL lets readers proceed during a write and makes commits cheaper; a
busy timeout makes a waiting connection retry instead of failing at once. It matters for the dashboard reads and a growing number of accounts. It is a change to the database FILE, so it needs
its own approval, a backup first and a tested way back.

What the code does (only when `SQLITE_WAL=1`): on every new connection `PRAGMA journal_mode=WAL`, `busy_timeout=SQLITE_BUSY_TIMEOUT_MS` (15000), `synchronous=NORMAL`. Tests (`tests/test_security_hardening.py`) prove the
pragmas, that turning the flag off does NOT undo WAL (the mode is stored in the file), and that the rollback below works.

## Things to know first

* WAL adds two files next to the database: `mba_bot.db-wal` and `mba_bot.db-shm`. **A plain file copy of `mba_bot.db` is not a complete backup in WAL mode** (a test demonstrates the loss). Use SQLite's online backup.
* **Check the existing nightly backup script before enabling.** If it copies the file with `cp`/`rsync`, change it to `sqlite3 /opt/mba-bot/mba_bot.db ".backup '<dest>'"` first. I have not seen the script.
* WAL needs a local filesystem (fine on the VPS). Do not enable it on a network share.
* Do it with no open trade and the service stopped.

## Enable (owner, one step at a time)

1. Confirm no open or unresolved trade: `sqlite3 'file:/opt/mba-bot/mba_bot.db?mode=ro' "select count(*) from trades where status not in ('closed','void');"` must print 0.
2. Back up (online, safe): `sqlite3 /opt/mba-bot/mba_bot.db ".backup '/opt/mba-bot/backup/pre-wal-$(date +%F-%H%M).db'"` then `sqlite3 <that file> "pragma integrity_check;"` must print `ok`.
3. Fix the backup script to use `.backup` (see above) and run it once by hand; check the result with `integrity_check`.
4. `sudo systemctl stop mba-bot`.
5. Add `SQLITE_WAL=1` to the service environment (owner edits `.env`).
6. `sudo systemctl start mba-bot`; wait one scheduler round.
7. Verify: `sqlite3 'file:/opt/mba-bot/mba_bot.db?mode=ro' "pragma journal_mode;"` prints `wal`; `ls /opt/mba-bot/mba_bot.db*` shows the `-wal` and `-shm` files; `journalctl -u mba-bot -n 50` shows normal rounds with no "database is locked".

## Roll back (service stopped)

1. `sudo systemctl stop mba-bot`.
2. `sqlite3 /opt/mba-bot/mba_bot.db "pragma wal_checkpoint(TRUNCATE); pragma journal_mode=DELETE;"` must print `delete`; the `-wal`/`-shm` files disappear.
3. Remove `SQLITE_WAL` from the environment (or set it to 0).
4. `sudo systemctl start mba-bot`; verify `pragma journal_mode;` prints `delete`.
5. If anything looks wrong with the data, restore the step 2 backup of the Enable section while the service is stopped.
