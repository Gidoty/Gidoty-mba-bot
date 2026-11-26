# Funding never blocks a close, a stop move or a protection check

Funding is a reporting figure and a small input to the cost buffer of the profit lock. It must never be the reason a trade close is not recorded or a stop is not managed.

## What changed (failure handling only)

- `lifecycle.read_funding` (called from `_finalize`, so on every close): the account id is read before the `try` through a helper that cannot raise, the handler reads no ORM attribute, and the log call is guarded. An expired object that cannot be reloaded ("database is locked") now gives `None` (funding unknown, stored as NULL, as before for any failed read) instead of an error out of the close.
- `lifecycle.refresh_funding` (called from `trader._live_cost_buffer`, on the profit-lock path): the commit that saves the figure (`_set`) can no longer raise into the lock path. If the save fails, it is logged and swallowed, the figure just read from the exchange is still used for that call, and the next call reads again. Any other failure returns what is stored, or 0.0, as the function already promised.
- `_safe_account_id` and `_safe_log` are small helpers that never raise.

Not changed: which trades are closed, when, at what price; stop and exit logic; the read interval (30 minutes); the stored values when nothing fails.

## Limits

When a save of the funding figure fails, `_set` rolls the session back (as it always did), which also discards other uncommitted changes in that transaction (for example a pending peak price). Both are re-derived on the next round.

## Tests

`tests/test_funding_never_blocks_close.py`: an expired account row, a failing commit, a failing rollback, a failing log call, and a real Futures close recorded while the funding read fails and the account row cannot be reloaded.
