# EMA 4h and 1d as one account: results (Part 2)

Plan hash `90e3369f6230d1b5` (committed before the run). Full tables: ema_portfolio_report.md and ema_portfolio_results.json. Same simulated trades as the universe study, development windows only (2022-04 to 2025-03); the confirmation windows were not read. Nothing here is a pass or a test; it describes the trades. The signal counts are a few above Part 1's (1,087 against 1,083 on 4h 20/50) because Part 1 counts only entries inside the six 182-day windows and this part counts the days between them too.

Risk per trade 1.0% of equity (the bot's default risk percent). Equity changes when a trade closes; open trades are not marked to market. The rule: the first signal takes a free slot (ties by catalog order), a signal that arrives while every slot is full is dropped.

## What one slot costs (EMA 4h 20/50 as the example)
| rule | trades taken (of 1,087 signals) | taken per year | total net R [99% interval] | mean net R | max drawdown | longest losing streak | months to 100 trades |
|---|---|---|---|---|---|---|---|
| 1 slot | 129 (12%) | 43 | +94 [-53, +400] | +0.73 | 20% of equity, 22 R | 16 | 28 |
| 2 slots | 275 | 92 | +99 [-103, +433] | +0.36 | 28%, 32 R | 13 | 13 |
| 3 slots | 392 | 131 | +163 [-117, +605] | +0.42 | 37%, 46 R | 13 | 9 |
| unlimited | 1,087 | 362 | +307 [-222, +1,000] | +0.28 | 48%, 64 R | 27 | 3.3 |

**The single slot drops 88% of the 4h 20/50 signals and the dropped trades carry +213 R of the +307 R total.** The reversal system is almost always in a trade on every pair, so a slot taken by the first signal is busy for the average hold (8 days on 4h, 31 days median on 1d) and most later signals arrive while it is full.

## All four cells, 1 slot against unlimited
| cell | signals | taken with 1 slot | taken per year | total net R, 1 slot | total net R, unlimited | R carried by the dropped trades | max drawdown, 1 slot | months to 100 trades, 1 slot |
|---|---|---|---|---|---|---|---|---|
| 4h 20/50 | 1,087 | 129 | 43 | +94 [-53, +400] | +307 | +213 | 20% | 28 |
| 4h 12/26 | 1,942 | 221 | 74 | +41 [-40, +143] | +309 | +268 | 19% | 16 |
| 1d 20/50 | 160 | 27 | 9 | -8 [-26, +19] | +83 | +91 | 11% | 133 |
| 1d 12/26 | 280 | 36 | 12 | +7 [-17, +35] | +101 | +94 | 7% | 100 |

Every interval spans zero. The one-slot total is below the unlimited total in all four cells; it is also lower in trades by 83% to 88%.

## Risk assumed
- 1.0% of equity per trade. With unlimited slots up to 9 or 10 positions are open at once, an open risk of 9% to 10% of equity; that is above the account maximum (2.0% per trade) and the paper total-risk cap (2.0%), so the unlimited rows are a description of the signals, not something the bot could place. With 1 slot the open risk is 1%, with 3 slots up to 3%.
- Drawdown at 1% risk (peak to trough of closed-trade equity): 4h 20/50 20% (1 slot) to 48% (unlimited); 1d cells 7% to 34%. The JSON has 0.5% and 2.0%.
- Longest run of losing trades in closing order: 11 to 27 on 4h (4 to 20 on 1d). The win rate is 15% to 33% and the median trade is -0.55 to -1.02 R. A few winners carry the total: removing the 5 best trades makes the rest of the total negative in most rows (share above 100%).
- Final equity multiples (0.92 to 9.5) are what these simulated trades would have done in these three years at 1% risk. They are in-sample, driven by a few trades, and are not an estimate of anything to come.

## Where I am unsure
- Auto Select ranks EMA signals by its own score in the live code; this study uses scan order (first signal in time, then catalog order). A different pick rule changes which trades are taken.
- The simulated reversal (close on the opposite crossover) frees the slot at the end of the exit candle; the live bot's exit and re-entry timing within a 5-minute round differ by minutes, not modelled.
- Closed-trade equity ignores unrealised losses, so true intra-trade drawdowns are larger than shown.
- The dropped-signal rule treats the live 10-minute entry window as strict; a bot that came up later would also drop signals.
- 1d on a single slot has 27 to 36 trades in three years; its figures rest on very few trades.
