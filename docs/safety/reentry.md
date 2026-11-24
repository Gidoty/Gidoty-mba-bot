# Profit protection v2, Part J: re-entry after a protective exit

Status: built, OFF (`REENTRY_ENABLED=false`). Base branch: `feature/ema-reversal-exits` (Part E), then G, F, D, C, B, A, and `feature/ema-exit-rules` (PR #84).

## What it is
After an EMA trade leaves through a protective exit, the trend can carry on without a new EMA crossover, and the crossover rule never enters again. With the switch on, the bot may try ONE more entry on the same pair and side. It is an ORDINARY entry: the same `_maybe_enter` call as a crossover entry, so the kill switch, daily limits, strategy guards, fee guard, pre-entry protection check, leverage cap, risk per trade and the protective stop placed and verified right after the fill all apply, and it takes the single live slot. The new trade carries `trades.reentry_of` (the id of the trade it followed, migration `b8e4a2f6d1c3`, not run by Claude, must run before the new code starts). A chain of re-entries is one trend.

## The conditions (`reentry.decide`, every one must hold)
1. The previous EMA trade on this pair closed with `break_even_stop`, `trailing_stop` or `profit_lock`. A plain stop-out, a time stop, a reversal exit or an opposite crossover never leads to a re-entry (those say the trend failed). A trade of another strategy never does either.
2. Between `REENTRY_COOLDOWN_CANDLES` (2) and `REENTRY_WINDOW_CANDLES` (48) closed candles of the EMA timeframe have started since it closed.
3. Fewer than `REENTRY_MAX_PER_TREND` (1) re-entries already follow this trend. A new EMA crossover entry starts a new trend (its `reentry_of` is empty).
4. The trend is intact: the fast EMA is on the trade's side of the slow EMA on the last closed candle, and the market is not flat (the same flat filter the crossover uses).
5. The last closed candle closed beyond the previous exit price by at least `REENTRY_ABOVE_EXIT_ATR` (1) x ATR: a new high above the previous exit level for a long, a new low below it for a short.
6. That candle closed no more than `EMA_ENTRY_WINDOW_MINUTES` ago (the freshness rule of a crossover entry).
Where a real crossover shows, the ordinary entry runs and nothing here changes it.

## How it interacts with the stop cooldowns (not a shortcut around them)
`EMA_STOP_COOLDOWN_HOURS` blocks a new entry after a plain `stop_loss` close only. A protective exit is not a stop-out to it, so the cooldown does not block a re-entry after a trailing stop, and a re-entry needs no cooldown exemption. A plain stop-out is not eligible for a re-entry at all, so the cooldown keeps blocking everything after it. Both are tested. The re-entry's own spacing is `REENTRY_COOLDOWN_CANDLES`, which is separate and not a stop cooldown.

## Scope and limits
- Single-slot accounts only (the live default). Accounts on `LIVE_MULTI_POSITION` with more than one slot, Auto Select pair changes and the multi-trade paper path are not covered.
- A re-entry pays a second round trip of fees and slippage. Part H counts it.
- Nothing here has been shown to make money; Part H reports the number of re-entries that recovered the move and the net effect.
