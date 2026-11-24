# Profit protection v2, Part B: profit rules every round, with bounded exchange calls

Status: built, switch OFF. Base branch: `feature/profit-peak-tracking` (Part A), which is based on `feature/ema-exit-rules` (PR #84).

## What changes with `PROFIT_EVERY_ROUND=true`
Today the Donchian trail and the EMA trail (PR #84) read CLOSED 1h candles, so a stop only moves when an hour closes. With the switch on, each round (every 5 minutes) the level is worked out from the running best price (`trades.peak_price`, Part A), which already includes the still-forming candle and the live price:
- EMA: break-even (the entry price) once the peak is up `EMA_BREAKEVEN_R`, and the trail (peak minus `EMA_TRAIL_ATR` x ATR) once it is up `EMA_TRAIL_TRIGGER_R`. These are PR #84's switches; with them off this part does nothing for EMA.
- Donchian: peak minus the strategy's own trail distance (`donchian_trail.running_level`), with the latest closed ATR. `donchian.py` is untouched (its SHA-256 is part of the pre-registered plan).
- RSI has no trail. Part C adds the shared staged lock for all three.

With the switch off, every path is exactly what #84 and the live Donchian code do today.

## One decision per round (`profit_protect.decide`)
1. No level, or a level not strictly better than the resting stop: hold. A stop is never loosened and never re-placed at the same level.
2. The price is at or through the level (at or below it for a long): the lock was earned and the price is already past it, so the trade exits through `lifecycle.execute_exit` with the rule's reason (`trailing_stop` or `break_even_stop`). This exit is never delayed by the limits below.
3. Otherwise the move must be worth its calls: at least `PROFIT_MOVE_MIN_IMPROVE_ATR` (default 0.25) x ATR better than the resting stop (0.1 R if no ATR is known). It must also be at least `PROFIT_MOVE_MIN_INTERVAL_SECONDS` (default 900, clamped 60 to 86400) after the last move. The first move that takes the stop off its loss side (the stop still worse than the entry) is exempt from the interval, not from the improvement.
4. A move goes through `lifecycle.move_stop`: the new stop is placed and read back resting first, the trade records it, then the old one is cancelled. Favourable direction only, never on the wrong side of the price, a failed move keeps the old stop.

`trades.stop_moved_at` (new nullable column, migration `e5b2c8d4f1a7`, not run by Claude, must run before the new code starts) is stamped inside `move_stop`'s own compare-and-set.

## Call budget (measured on the fake client; real Binance weights UNVERIFIED)
A round that holds costs 9 calls for an open Futures trade (unchanged from today). A round that moves a stop costs 14: 5 more (one position read, two order reads, one create, one cancel).

Worst case with the defaults: one first-lock move plus one per 900 s = 5 moves per trade per hour = 25 extra calls per trade per hour.
- 20 accounts, one live trade each: 500 extra calls per hour (0.14 per second).
- 100 accounts, one live trade each: 2,500 extra calls per hour (0.7 per second).
- With `LIVE_MULTI_POSITION` and 3 slots per account, multiply by 3.
A shorter interval raises this linearly; the 60 second floor means at most 61 moves per hour per trade, which is why the floor exists.

## What this does not fix
- A level is only as good as the round that sees it: the bot checks every 5 minutes plus the round time. A fall between two rounds goes through the level before the stop moves. Only the stop already resting protects that move.
- Exit at market when the price is through the level means a worse fill than the level in a fast market. Not a guarantee.

## Rollback
Set `PROFIT_EVERY_ROUND` off (or unset) and restart. The next round uses the closed-candle paths again. The column stays, unused.
