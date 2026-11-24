# EMA on 4h and 1d across the pair catalog: results

Plan hash `bc1f9d088852a3c3`, committed before the run (docs/research/ema_universe_preregistration.md). Full tables: ema_universe_dev_report.md and ema_universe_dev_results.json. These are measurements on simulated trades, not evidence of profit.

## Answer
**No cell passes the pre-registered development rule.** The confirmation windows were not opened and no look was recorded.

| cell | trades (2022-04 to 2025-03) | mean net R | total net R, 99% interval | windows > 0 of 6 | pairs > 0 of 10 | failed |
|---|---|---|---|---|---|---|
| EMA 4h 20/50 | 1,083 | +0.278 | +301 [-219, +997] | 4 | 9 | P2, P3 |
| EMA 4h 12/26 | 1,929 | +0.158 | +306 [-203, +1,016] | 4 | 9 | P2, P3 |
| EMA 1d 20/50 | 160 | +0.519 | +83 [-61, +299] | 5 | 6 | P2 |
| EMA 1d 12/26 | 279 | +0.328 | +92 [-76, +324] | 5 | 7 | P2 |

All four point estimates are positive and the interval of the total spans zero in all four. The 4h cells are positive in 4 of 6 windows (rule: 5). The 1d cells meet the window and pair rules, but their intervals reach -61 and -76 R. The positive pairs: 4h 20/50 is positive on 9 of 10 pairs (LTC is the exception, -27 R); the best pair (XRP, +80 R) is not what carries it: without it the total is still +221 R.

## What the trades look like
- Gross R per trade before costs: +0.36 (4h 20/50), +0.23 (4h 12/26), +0.63 (1d 20/50), +0.39 (1d 12/26). Costs take 0.07 to 0.12 R per trade at these stop distances (4.8% on 4h, 12% on 1d), against 0.15 to 0.20 R on 1h.
- Fees paid in BNB add +0.003 R per trade on 4h and +0.001 on 1d, so the verdict does not move.
- A few trades carry the totals. In the 1d 20/50 cell, over 2020-04 to 2025-03, one DOGE long held 302 days earned +781 R, and five trades make 96% of the total; the median trade is -0.93 R. In 4h 20/50, over the same span (1,645 trades), the top 5 trades make 43% of the total and the median trade is -1.00 R. The supplementary pre-window history (2020-04 to 2022-03; no rule reads it) shows +0.64 R per trade on 4h 20/50 with an interval above zero (+37 to +786 total), and a 1d mean of +14.8 R that is mostly the one DOGE trade. That history is the bull market of 2020 and 2021 and was not part of the rule.

## Trades per year (counted 2020-04 or the pair's first candle to 2025-03; entries, no confirmation-window trade)
| cell | all 10 pairs | 1 pair (average, slowest, fastest) | the 4 original pairs |
|---|---|---|---|
| EMA 4h 20/50 | 356 | 35.6 (30.5 DOGE, 41.4 LTC) | 145 |
| EMA 4h 12/26 | 644 | 64.4 (52.7, 72.2) | 261 |
| EMA 1d 20/50 | 48 | 4.8 (3.1 SUI, 7.2 XRP) | 21 |
| EMA 1d 12/26 | 87 | 8.7 (5.2, 11.0) | 38 |

## Months to 100 closed trades, if every signal is taken (unlimited slots; Part 2 gives the one-slot figures)
| cell | 1 pair (average) | 1 pair (slowest, fastest) | 4 original pairs | 10 pairs |
|---|---|---|---|---|
| EMA 4h 20/50 | 33.7 | 39.4, 29.0 | 8.3 | 3.4 |
| EMA 4h 12/26 | 18.6 | 22.8, 16.6 | 4.6 | 1.9 |
| EMA 1d 20/50 | 249 | 382, 167 | 56 | 24.9 |
| EMA 1d 12/26 | 139 | 229, 109 | 32 | 13.9 |

## Positions wanted at once (2023-09 to 2025-03, all 10 pairs active, every signal taken)
The EMA strategy is a reversal system: a pair is nearly always long or short. The mean number of open positions is 8.1 (4h 20/50), 8.9 (4h 12/26), 7.5 (1d 20/50) and 8.7 (1d 12/26) out of 10; at least one position is open 100% of the time, at least two 100% of the time, and the most open at once is 9 or 10. The live bot has one slot today (multi-position exists behind a switch, up to 3).

## Windows
Development windows 0 to 5 (2022-04 to 2025-03) were read. **Windows 6, 7 and 8 (2025-04 to 2026-09) remain unread for every 4h and 1d cell on all 10 pairs**, as they were after #105. The ledger file (`sealed_looks.ema_universe.json`) does not exist. The 1h studies did read those calendar months for four pairs at 1h.

## Where I am unsure
- The catalog has 10 pairs; the owner asked for at least 12 if available. Not available without leaving the catalog.
- The USD 50 million line was set after I saw the volumes (all pairs clear it by a wide margin, so it filtered nothing).
- Slippage 11 bps round trip is an assumption; on 4h and 1d trades with 5 to 12% stops it matters far less than on 1h. Stop-order slippage in fast markets is not measured.
- Funding and candles come from the public archive and are not checked against the live API. 4h and daily candles are built from 1h candles.
- The pairs are correlated (one market move often moves most pairs), so 10 pairs are fewer than 10 independent tests; the bootstrap resamples months, which keeps same-month pairs together.
- The simulator holds one position per pair and reverses on the opposite crossover with no re-entry rule; the live bot's entry window (10 minutes after the candle closes) and the 5-minute rounds are not modelled.
- Four cells were tested; the 99% level is the only multiple-testing guard.
