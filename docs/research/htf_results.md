# Higher timeframes: results

Plan hash `235575505fe930be` (registered in commit `0f903b9` before any run). Data hash `893e32f96ccd0063`. 4h and 1d candles built from the 1h history, BTC, ETH, BNB, XRP pooled, own exits, 5 bps taker per side, 11 bps round-trip slippage, historical funding, 99% month-block intervals. Development windows 2022-04 to 2025-03 (six 182-day windows). Full table: `docs/research/htf_dev_report.md`. **A measurement, not evidence of an edge.**

## The answer
**Nothing passes the pre-registered development rule, so the confirmation windows (2025-04 to 2026-09) stay unread and no look was recorded.** One cell met P3 (Donchian 4h with the 55/20 channel: positive in 5 of 6 windows) and none met P2: every interval of the total includes zero.

| cell | trades | per year, 4 pairs (per pair) | avg stop distance | gross R | cost R | mean net R | total net R [99% interval] | windows > 0 (of 6) |
|---|---|---|---|---|---|---|---|---|
| EMA 4h 20/50 | 456 | 145 (36) | 3.73% | +0.453 | 0.089 | **+0.363** | +165.6 [-100.4, +528.2] | 4 |
| EMA 4h 12/26 | 793 | 261 (65) | 3.77% | +0.318 | 0.080 | +0.238 | +188.8 [-98.5, +570.4] | 4 |
| EMA 1d 20/50 | 72 | 21 (5.4) | 9.97% | +0.834 | 0.117 | **+0.717** | +51.6 [-53.0, +220.7] | 3 |
| EMA 1d 12/26 | 115 | 38 (9.4) | 9.82% | +0.549 | 0.062 | +0.487 | +56.0 [-35.9, +214.6] | 3 |
| Donchian 4h 20/10 | 847 | 279 (70) | 2.89% | +0.099 | 0.092 | +0.007 | +6.2 [-87.4, +104.2] | 3 |
| Donchian 4h 55/20 | 490 | 167 (42) | 3.02% | +0.169 | 0.090 | +0.079 | +38.9 [-37.5, +119.5] | 5 |
| Donchian 1d 20/10 | 128 | 42 (10.5) | 7.65% | +0.072 | 0.051 | +0.021 | +2.7 [-43.2, +63.0] | 3 |
| Donchian 1d 55/20 | 80 | 25 (6.3) | 7.83% | +0.167 | 0.057 | +0.110 | +8.8 [-24.9, +54.3] | 3 |
| RSI 4h | 120 | 40 (10) | 2.75% | -0.002 | 0.083 | -0.002 | -0.2 [-25.1, +24.2] | 4 |

## What it says
- **The mechanism is real.** The cost per trade in R falls as the stop widens: about 0.15 to 0.20 R on 1h, 0.08 to 0.09 R on 4h (stop 2.8% to 3.8%) and 0.05 to 0.12 R on 1d (stop 7.7% to 10%). A trade costs less of the amount risked on a wider stop.
- **EMA on 4h and 1d has positive point estimates in all four cells** (+0.24 to +0.72 R per trade) with gross edges of +0.32 to +0.83 R, much larger than on 1h in the same years. They are not distinguishable from zero: the intervals of the total run from about -100 to +570 R (4h) and -53 to +221 R (1d), and the gain comes from a few windows (the two or three best windows carry most of the total; 3 to 4 of 6 windows are positive). With 72 to 115 trades on 1d, a mean of +0.5 to +0.7 R has a bootstrap interval of roughly -0.3 to +2.6 R.
- **Donchian on 4h and 1d** is near zero to slightly positive (+0.007 to +0.110 R) with intervals around zero, and **RSI on 4h** is -0.002 R: the strategies' signals earn little before costs and costs now take about half of the small gross.
- **Trades per year is the practical limit.** One pair running EMA 4h makes about 36 trades a year (65 with the 12/26 set), EMA 1d about 5 to 9, Donchian 4h 42 to 70, RSI 4h about 10. A 100-trade pilot on one pair would take about 1.5 to 3 years on EMA 4h, 10 to 20 years on EMA 1d, and 1.4 to 2.4 years on Donchian 4h.

## Not done, and why
No cell passed development, so by the pre-registered protocol the confirmation windows are not opened and nothing was tuned. EMA 4h and 1d are the cells that would justify a further study with more data or a longer history (one cannot add more history than the archive has: this one starts in 2020); that would be a new pre-registration, not a re-read of these windows.

## Limits
Candle-built 4h and 1d bars equal the exchange's (UNVERIFIED against the live API); 9 cells at 1% (0.09 false passes expected); few trades per window on 1d; the same months were used by the 1h studies, so these windows are not unread calendar time, only unread for these timeframes; 5 bps fee and 11 bps slippage assumed (the 11 bps matter less here because the stop is wider).
