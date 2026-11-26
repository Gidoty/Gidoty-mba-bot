# The +1R profit lock

Status: built, OFF (`PROFIT_LOCK_R=0`). Depends on the net-P&L PR (it reads the account's real fee tier and the funding paid) and, through it, on the peak-price PR (Alembic chain and `trades.peak_price`). No migration of its own. It does not depend on PR #84 or any other parked PR.

## The rule (one switch)
`PROFIT_LOCK_R` (0 = off, clamped 0.25 to 50; unset, empty, malformed, zero, negative or non-finite means OFF, so a typo never switches it on; `1` is the owner's +1R). R is the distance from the entry to the INITIAL stop. Once the trade's best price has been at least that many R beyond the entry, the protective stop is moved to the entry plus the round-trip cost buffer:
`entry x (2 x taker fee + assumed round-trip slippage) + funding paid so far per unit`
- Taker fee: the account's real rate when it can be read (cached 24 hours, nothing stored), else the assumed `FEE_GUARD_FEE_BPS` (5 bps). Slippage: `FEE_GUARD_SLIPPAGE_BPS` (11 bps round trip). Funding: the Futures funding paid so far from the read-only income history (re-read at most every 30 minutes, only once the lock could act); funding received never lowers the buffer; a failed read counts as what is stored. A failed fee or funding read never blocks the lock (tested).
- Same code for EMA, Donchian and RSI trades, live and paper. All moves go through `lifecycle.move_stop` (the new stop is placed and read back resting before the old one is cancelled, favourable direction only, never on the wrong side of the price; a refused stop keeps the old one). The position is never without a stop.
- **The better stop wins.** If a stop already rests above the lock level (a Donchian trail), the lock does nothing. If the lock moves first and the Donchian trail later asks for a better level, `move_stop` accepts that, because it is better. Neither ever loosens the other.
- **A level the price has already fallen through** (the trade was +1R, then dropped below entry plus the buffer before the stop could move): a stop there would be on the wrong side, so the trade exits at the market through `lifecycle.execute_exit` with the close reason `profit_lock`. This is the behaviour the sealed test simulated ("a price already through the level at the candle close leaves at that close"). It is a market exit near the entry instead of waiting for the original stop.
- A level not below the best price is never used: when R is smaller than the buffer, the lock waits until the gain exceeds the buffer.
- A `profit_lock` exit is not a stop-out for the EMA stop cooldown (that counts plain `stop_loss` only).

## When it is judged (second switch)
`PROFIT_LOCK_EVERY_ROUND` (false by default).
- **false, closed candles (the default and the mode the sealed test simulated):** the best price is what the strategy's CLOSED candles after the entry show. The decision depends on the candles alone; the level can change only when a candle closes or a funding settlement raises the buffer.
- **true, every round (5 minutes):** the best price includes the live price and the forming candle (`trades.peak_price`). **This was NOT simulated.** It can lock earlier inside a candle, and it can also exit trades the closed-candle mode would have kept. To bound exchange calls it needs `PROFIT_MOVE_MIN_IMPROVE_ATR` (0.25) and `PROFIT_MOVE_MIN_INTERVAL_SECONDS` (900, min 60): a later refinement must improve the stop by that much and wait that long; the FIRST lock (stop still worse than the entry) is never delayed. Worst case 5 moves per trade per hour, about 5 exchange calls each (measured on the fake exchange: a move round costs 5 more calls than a holding round); 20 accounts with one trade: 500 extra calls per hour, 100 accounts: 2,500 (Binance weights UNVERIFIED). The spacing state is in memory, so one extra move is possible after a restart.

## What the sealed test says (PR #96, EMA only; Donchian and RSI in the later study)
The plain +1R lock cut the give-back rate from 50% and 64% to 2% and 6% on 1h, but its net effect was -5.4 R on the tune window and +33.2 R on the confirmation window against the current rules: not distinguishable from noise. The owner chose to run it for his counted pilot so every trade follows one rule set.

## Spot
The new stop is placed before the old one is cancelled, which needs coins the old stop holds. The fake exchange refuses it on Spot (UNVERIFIED on real Binance Spot); the old stop then stays and the customer gets the existing "could not move the trailing stop" notice at most once per 6 hours. The market exit through `execute_exit` works on Spot.

## Rollback
Set `PROFIT_LOCK_R` to 0 or unset it and restart. Stops already moved stay where they are (they are ordinary protective stops).
