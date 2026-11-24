# Profit rules for Donchian and RSI: sealed test results

Plan hash `56e8551ce679ce4a` (registered in commit `1650815` before any run), data hash `893e32f96ccd0063`. 1h candles and historical funding, BTC, ETH, BNB, XRP pooled. In-sample window 2020-04-01 to 2025-01-01 (**57 months**), confirmation window 2025-01-04 to 2025-11-01 (**10 months**, read ONCE, recorded in the ledger before it was read; no earlier study had read any of it). 2025-11 to 2026-10 was not used (see the pre-registration). Costs: 5 bps taker per side, 11 bps round-trip slippage, historical funding. Intervals are 95% bootstrap intervals (2000 resamples); the month-block interval is the one to read. This is a **measurement on two windows, not evidence that a rule will make money.**

## The answer
1. **Both strategies lose money after costs in both windows, and the loss is distinguishable from noise.** Donchian: -0.173 R per trade over 5,550 trades in the 57 months (95% interval -0.206 to -0.139; total -959 R, month-block interval -1,200 to -718), and -0.166 R over 1,012 trades in the 10 unread months (-0.251 to -0.074; total -168 R, -266 to -62). RSI: -0.266 R over 655 trades (-0.326 to -0.201; total -174 R, -221 to -128) and -0.215 R over 113 trades (-0.371 to -0.060; total -24 R, -57 to +3). The profit rules are small next to that.
2. **The +1R lock does nothing for RSI, by construction.** RSI's stop is 1.5 ATR and its profit target is 1.0 ATR, so the target (+0.67 R) fills before the best price can reach +1 R. All three RSI rule sets gave identical trades in both windows. Switching `PROFIT_LOCK_R=1` on for an RSI trade changes nothing.
3. **The +1R lock lowers Donchian's net result, and in the 57-month window the difference is distinguishable from noise: -55.6 R (per-entry interval -0.0197 to -0.0001 per entry, month-block -106 to -6).** It cuts Donchian's give-back rate from 33% to 11% (57 months; 9% against 33% in the confirmation window), at the cost of 149 winners cut short (35 in the confirmation window), and total net R -17.4 in the confirmation window (not distinguishable: month-block interval -38 to +0.3). Consistent sign in both windows: negative.
4. **PR #84's take profit, break-even and trail adapted to Donchian** came out +46.1 R in the 57-month window and -21.5 R in the confirmation window, neither distinguishable from noise (month-block intervals -42 to +128 and -50 to +10). Opposite signs: no conclusion. For RSI it also changed nothing (its break-even and trail activate beyond its target).
5. Put with PR #96: the lock lowers the give-back rate every time, and no study has found that it raises net profit. For EMA it was a wash (-5.4 R, then +33.2 R), for Donchian it was negative both times (distinguishable in the large window), for RSI it is inert.

## Numbers (all windows)
| strategy | rule set | window | trades | avg net R [95% CI] | total net R | vs RS0 total R [month-block CI] | give-back rate | winners cut short |
|---|---|---|---|---|---|---|---|---|
| Donchian | current rules | 57 months | 5,550 | -0.173 [-0.206, -0.139] | -959.4 | - | 33% | 0 |
| Donchian | +1R lock | 57 months | 5,667 | -0.179 [-0.212, -0.146] | -1,014.9 | -55.6 [-105.8, -5.7] (distinguishable) | 11% | 149 |
| Donchian | #84 style | 57 months | 6,022 | -0.152 [-0.181, -0.121] | -913.3 | +46.1 [-42.4, +128.2] | 35% | 196 |
| Donchian | current rules | 10 months | 1,012 | -0.166 [-0.251, -0.074] | -167.9 | - | 33% | 0 |
| Donchian | +1R lock | 10 months | 1,039 | -0.178 [-0.258, -0.097] | -185.3 | -17.4 [-38.0, +0.3] | 9% | 35 |
| Donchian | #84 style | 10 months | 1,105 | -0.171 [-0.239, -0.098] | -189.3 | -21.5 [-50.4, +9.5] | 37% | 34 |
| RSI | all three rule sets | 57 months | 655 | -0.266 [-0.326, -0.201] | -174.2 | +0.0 | 2% | 0 |
| RSI | all three rule sets | 10 months | 113 | -0.215 [-0.371, -0.060] | -24.3 | +0.0 | 10% | 0 |
(Maximum drawdowns at 0.5% risk per trade are 99% to 66% for Donchian and 59% and 13% for RSI: the strategies lose money.)

## What this means for the pilot
- The lock is safe to leave on for RSI (inert) and costs Donchian a little net R for a large cut in give-back. For EMA it was a wash. If every trade is to follow one rule set, that is the cost of consistency; it is not an edge.
- The bigger fact is the baseline: Donchian and RSI, as they run today, lost money after costs and funding in 5 years and in the 10 unread months. Part 3 asks whether any entry rule changes that.

## Limits
Candle-close simulation (the live bot acts every 5 minutes); the lock level here has no funding term (slightly optimistic against live); the exchange-call limits, real stop fills and the account's real fee tier are not modelled (5 bps assumed); both strategies' parameters are the shipped defaults; archive candles and funding may differ from the live API (UNVERIFIED). Per-entry intervals treat trades as independent and are too narrow.
