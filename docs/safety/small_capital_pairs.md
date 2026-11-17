# Small-capital pairs (B3)

Status: on a branch, not merged, not deployed. Ships OFF (`SMALL_CAPITAL_PAIRS`, default off). CLAUDE-RUN: tests pass against a FAKE exchange; no Binance minimum was read or assumed.

## What the bot does with the switch OFF
Exactly what it does today: no row is written, `AccountOut` carries no pair message, `/api/pairs` answers as before, Auto Select scans as before (the capital skip only for live Futures with the expansion on, using `MAX_NOTIONAL_MULTIPLIER`), and the entry path refuses an order below the exchange minimum with the existing reason codes (`below_exchange_minimum_or_filters`, `size_zero_after_ceiling`).

## What changes with the switch ON (every account, one code path)
* **The minimum order** is read only from the exchange filters the bot already loads: `MIN_NOTIONAL` or `NOTIONAL`, and the quantity floor (the larger of the market minimum quantity and one step) at the current price when a price is at hand (`small_capital.min_order_value`). A part that cannot be read is left out; if nothing can be read the pair is NOT skipped (the entry path still checks in full).
* **Capital needed** = that minimum / the customer's leverage cap (the stored `max_leverage`, default 3, never above `FUTURES_MAX_LEVERAGE`; 1 on Spot). It is a necessary condition: it never marks a pair unviable that the entry path could trade.
* **A skip is recorded** in `pair_skips`: one row per account, pair, reason and UTC day with a count and the numbers (capital, leverage cap, capital needed). Reasons: `pair_below_minimum_capital` (Auto Select skip, or the sized quantity is zero) and `pair_below_exchange_filters` (the entry path's filters refused the quantity). Live accounts only; paper writes nothing.
* **The customer** sees a plain text on the dashboard and in Bot Settings (Trading tab) for the account's own pair while a skip from the last 24 hours exists: "<pair> is too small for your trading capital. Its smallest order needs about N USDT of trading capital at your Kx leverage limit. Your trading capital is C USDT. Choose another pair or raise your trading capital." `GET /api/pairs` (Futures, expansion on) lists an unviable pair as unavailable with the same number.
* **Auto Select** scans only pairs viable for the account (every live account, Spot included), recording the others. It never forces a pick.
* **Report:** `python -m mba_bot.pair_skip_report [--days N]` (default 7). Read-only (`query_only`), no exchange call, prints the first 8 characters of an account id and numbers only. Exit 0 printed, 2 unreadable.

## Migration `f4c7d9b2a6e1` (parent `e3b6c8a1d5f2`)
One new table `pair_skips`, additive, idempotent, reversible (the downgrade drops it and the skip history). NOT run.

## Owner steps (the owner runs every one)
1. `python -m mba_bot.restart_gate` must exit 0. Stop, back up, `alembic upgrade head`, deploy, start.
2. Check health. The switch is still off.
3. Set `SMALL_CAPITAL_PAIRS=true`, restart gate, restart. After a day run `python -m mba_bot.pair_skip_report`.
Rollback: remove the line and restart. `alembic downgrade e3b6c8a1d5f2` drops the table.

## NOT verified
* Any real Binance minimum. The fixtures set their own; the bot reads the live values from the exchange info at run time.
* The effect on a real Auto Select account (the 4-pair universe at small capital): not observed.
* The quantity floor at Auto Select time uses the minimum notional only (no price is at hand before candles are fetched); the entry path adds the price-based floor.
* The screens in a browser.
