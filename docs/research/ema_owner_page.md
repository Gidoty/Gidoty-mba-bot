# EMA on 4h and 1d: one page for the owner

Facts and limits only. This page does not say whether to trade. All figures are simulated trades on archive candles, 10 pairs, 2022-04 to 2025-03, with 5 bps fees per side, 11 bps round-trip slippage and historical funding. Plans: `bc1f9d088852a3c3`, `90e3369f6230d1b5`.

## What the evidence says
- **No version passed the test set in advance.** EMA 20/50 (the shipped setting) and 12/26, on 4h and 1d, each averaged +0.16 to +0.52 R per trade, but the 99% interval of the total includes zero in all four. The two 4h versions were positive in 4 of 6 half-years; the rule asked for 5.
- **Before costs** the trades averaged +0.23 to +0.63 R. Costs take 0.07 to 0.12 R on these timeframes, against 0.15 to 0.20 R on 1h.
- **It wins rarely and the total comes from a few trades.** About 1 trade in 4 wins and the median trade loses about 1 R (4h 20/50: -1.01 R). Over 2020 to 2025 one DOGE trade held 302 days made 72% of the 1d 20/50 total.
- **Trades per year, 4h 20/50:** about 36 per pair, 356 across 10 pairs. On 1d 20/50: about 5 per pair, 48 across 10 pairs.
- **One slot loses most signals.** The strategy is nearly always in a trade on each pair (about 8 of 10 on average). With one slot, first signal wins, 88% of 4h 20/50 signals are dropped, and the dropped trades carried +213 R of the +307 R total. One-slot trades per year: 43 (4h 20/50), 9 (1d 20/50).
- **100 closed trades take:** 28 months (4h 20/50, 1 slot), 9 (3 slots); 133 months (1d 20/50, 1 slot), 62 (3 slots).
- **Drawdown at 1% risk per trade:** 20% of equity with 1 slot, 37% with 3 slots; longest run of losing trades 13 to 16.

## What the live bot would need
- **Timeframe:** `TIMEFRAME` is one setting for the EMA scan; 4h and 1d are accepted values. Changing it is an env change and a restart (yours). Settings counted in candles (time stop, re-entry) change meaning.
- **Closed candles:** `EMA_CLOSED_CANDLE_ONLY` is off by default. Off, the bot reads a forming 4h or 1d candle for hours.
- **Entry window:** 10 minutes after the candle closes, rounds every 5 minutes. A bot that is down at that moment skips the signal; on 1d the next one is days or weeks away.
- **Protection check:** the 2 ATR stop is about 4.8% from entry on 4h and 12% on 1d. The check refuses a stop beyond the liquidation price, so the account's borrowing multiplier must stay low (roughly under 20x on 4h and 8x on 1d, my arithmetic, not read from the exchange). At 1% risk the position is about 21% (4h) or 8% (1d) of equity, which can fall below a pair's minimum order on a small account.
- **Slots:** one today. Auto Select refuses more than one slot. Multi-position is off by default, at most 3 slots, one per pair, open risk capped at 3%. Auto Select scans 4 pairs; this study used 10.

## Still unknown
- The confirmation windows (2025-04 to 2026-09) are unread for every 4h and 1d cell.
- Real slippage on stops, fees and funding on your account.
- Whether Auto Select's own pair ranking changes which trades one slot takes (the study used first signal in time).
- Whether the 2020 to 2021 trend years, which dominate the history, repeat.
