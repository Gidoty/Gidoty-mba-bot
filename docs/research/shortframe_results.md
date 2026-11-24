# Short-timeframe research: results

Registered plan hash `d8f9a600f51048b6` (commit 6bd510c, before any run). Candle data hash `9ff9d9da31e19a7d` (BTC, ETH, BNB, XRP USD-M Futures, 5m, 15m, 30m, 1h, 2025-10-01 to 2026-09-30, from Binance's public archive). Run on 24 Nov 2026 by `python -m mba_bot.shortframe_backtest tune` (9 seconds). No parameter was changed between registration and the run.

## Answer
**No strategy on any timeframe clears the pre-registered rule after costs, including the 1h control.** No cell qualified on the tuning window, so the confirmation window (2026-07-04 to 2026-09-15) was NOT read and no look was recorded. Nothing was tuned afterwards, as registered.

## What the table says
* **Costs alone are larger than any gross edge on every short timeframe.** Fees plus slippage cost between 0.14 R (EMA, 1h) and 0.86 R (RSI, 5m) per trade. The best gross expectancy in the whole table is +0.126 R (RSI 30m); several cells have a gross edge at or below zero before any cost.
* **The shorter the candle, the worse the net result.** EMA net R per trade: 1h -0.030, 30m -0.227, 15m -0.241, 5m -0.639. Donchian: 1h -0.094, 30m -0.235, 15m -0.453, 5m -0.835. RSI: 1h -0.185, 30m -0.160, 15m -0.501, 5m -0.867. The reason is arithmetic, not luck: a 2 ATR stop on a 5-minute candle is a tiny distance, so the same 0.21% round-trip cost is several R.
* **Trade frequency is real but not useful.** Pooled over the four pairs and with no slot limit (an upper bound), 5m Donchian trades 25 times a day and 5m EMA 21 times a day, and 99 to 100% of the 0.5%-risk equity is lost over 8 months in both. The only cells with modest drawdowns (RSI 1h 10.5%, RSI 30m 14.8%) are the ones with the fewest trades and a still negative net.
* **The 1h control also fails.** EMA 1h is the closest: net -0.030 R per trade, net-positive on BTC and ETH, negative on BNB and XRP; costs take 127% of a +0.113 R gross edge. This matches the earlier 1h research: the strategy has a thin edge before costs and none after them.

## Per cell, tuning window 2025-11-01 to 2026-07-01
| strategy | timeframe | trades | per day | avg hold (min) | win % | gross R | net R | cost per trade (R) | cost share of gross | worst losing streak | max DD % | pairs net-positive | rule |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ema | 5m | 5139 | 21.24 | 183 | 15.4 | +0.025 | -0.639 | 0.665 | 2635% | 63 | 100.0 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| ema | 15m | 1560 | 6.45 | 648 | 20.4 | +0.086 | -0.241 | 0.327 | 380% | 30 | 88.9 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| ema | 30m | 806 | 3.33 | 1278 | 21.2 | -0.011 | -0.227 | 0.217 | n/a% | 34 | 65.3 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| ema | 1h | 371 | 1.53 | 2842 | 23.2 | +0.113 | -0.030 | 0.144 | 127% | 29 | 31.4 | 2 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| donchian | 5m | 6079 | 25.12 | 30 | 17.8 | -0.005 | -0.835 | 0.830 | n/a% | 53 | 100.0 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| donchian | 15m | 2885 | 11.92 | 101 | 22.3 | -0.003 | -0.453 | 0.450 | n/a% | 41 | 99.9 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| donchian | 30m | 1528 | 6.31 | 224 | 28.7 | +0.068 | -0.235 | 0.303 | 445% | 30 | 85.1 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| donchian | 1h | 786 | 3.25 | 471 | 31.8 | +0.107 | -0.094 | 0.201 | 188% | 18 | 34.3 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| rsi | 5m | 1100 | 4.55 | 21 | 30.5 | -0.003 | -0.867 | 0.864 | n/a% | 30 | 99.2 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| rsi | 15m | 328 | 1.36 | 74 | 45.7 | -0.060 | -0.501 | 0.442 | n/a% | 8 | 56.4 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| rsi | 30m | 171 | 0.71 | 139 | 62.6 | +0.126 | -0.160 | 0.286 | 227% | 5 | 14.8 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |
| rsi | 1h | 100 | 0.41 | 290 | 57.0 | +0.021 | -0.185 | 0.206 | 988% | 6 | 10.5 | 0 of 4 | does not clear (net_expectancy, costs_leave_the_edge, positive_pairs) |


"n/a%" means the cell has no positive gross edge, so the share of it consumed by costs is undefined (and the rule cannot be met). Exits by reason and per-pair net R are in `tune_results.json` beside the data (not committed). Per-pair net-positive: only EMA 1h reached 2 of 4.

## Limits of this study (read before using it)
* The strategies are the live rules unchanged, applied to shorter candles. A different design built for short horizons (for example a wider stop in ATR terms, a volatility filter, or limit-order entries that avoid the taker fee) is NOT tested here and could behave differently. This study shows only that the existing three, as they are, do not survive costs.
* The 11 bps round-trip slippage and the 5 bps taker fee are assumptions (the fee guard's and the Futures standard); real fills at small size may be cheaper, a maker-fee design is not modelled, and funding is not modelled. Costs would have to fall by more than half on 15m and by more than 90% on 5m for any cell here to turn positive.
* One year of data, 8 months of it in the tuning window, one market regime sequence. Pairs are pooled with no slot limit.
* Indicators are computed over the whole series (the live bot uses the latest 200 candles) and RSI exits use a 400-candle warm-up: small, named differences, covered by tests.
* The confirmation window was never read, so nothing here was confirmed; the answer is "not even the tuning window clears the rule".
