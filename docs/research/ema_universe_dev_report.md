# EMA 4h and 1d on the catalog: development windows (2022-04 to 2025-03)

Plan hash `bc1f9d088852a3c3`. 10 catalog pairs pooled, shipped stop and crossover exit, 5 bps taker per side, 11 bps round-trip slippage, historical funding, 99% month-block bootstrap intervals.

| cell | trades | trades per year, 10 pairs | per pair per year | avg stop distance | avg gross R | avg cost R | mean net R | total net R [interval] | windows > 0 | pairs > 0 | total without best pair | P1 | P2 | P3 | P4 | passes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ema 4h s1 | 1083 | 355.8 | 35.6 | 4.75% | +0.357 | 0.078 | +0.278 | +301.3 [-219.0, +997.2] | 4 | 9 of 10 | +221.4 | yes | no | no | yes | no |
| ema 4h s2 | 1929 | 643.7 | 64.4 | 4.83% | +0.225 | 0.066 | +0.158 | +305.7 [-203.1, +1016.2] | 4 | 9 of 10 | +223.4 | yes | no | no | yes | no |
| ema 1d s1 | 160 | 48.1 | 4.8 | 12.20% | +0.634 | 0.116 | +0.519 | +83.0 [-61.3, +298.8] | 5 | 6 of 10 | +59.8 | yes | no | yes | yes | no |
| ema 1d s2 | 279 | 86.5 | 8.7 | 12.55% | +0.392 | 0.064 | +0.328 | +91.6 [-75.9, +324.2] | 5 | 7 of 10 | +54.3 | yes | no | yes | yes | no |

With fees paid in BNB (10% off the fee, nothing else changed):

| cell | mean net R | total net R [interval] | windows > 0 |
|---|---|---|---|
| ema 4h s1 | +0.281 | +304.1 [-216.0, +999.7] | 4 |
| ema 4h s2 | +0.161 | +310.5 [-198.3, +1020.9] | 4 |
| ema 1d s1 | +0.519 | +83.1 [-61.2, +299.0] | 5 |
| ema 1d s2 | +0.329 | +91.8 [-75.6, +324.6] | 5 |

Per window total net R (standard fees):

| cell | w0 | w1 | w2 | w3 | w4 | w5 |
|---|---|---|---|---|---|---|
| ema 4h s1 | +22.9 (171) | +52.0 (156) | -19.7 (186) | +167.8 (179) | -25.1 (176) | +103.3 (215) |
| ema 4h s2 | -36.5 (337) | +76.5 (299) | -17.9 (330) | +119.1 (316) | +35.4 (305) | +129.0 (342) |
| ema 1d s1 | +0.5 (21) | -20.3 (33) | +0.9 (31) | +53.8 (14) | +9.4 (33) | +38.7 (28) |
| ema 1d s2 | +7.5 (33) | -19.5 (43) | +13.3 (40) | +45.3 (44) | +1.3 (58) | +43.7 (61) |

Positions wanted at once if every signal were taken, 2023-09-01 to 2025-04-01 (all 10 pairs active):

| cell | mean open | share of time with 1 or more | with 2 or more | maximum |
|---|---|---|---|---|
| ema 4h s1 | 8.13 | 100% | 100% | 10 |
| ema 4h s2 | 8.94 | 100% | 100% | 10 |
| ema 1d s1 | 7.54 | 100% | 100% | 9 |
| ema 1d s2 | 8.68 | 100% | 100% | 10 |


Cells that pass development (P1 at least 150 trades; P2 mean above zero and the 99% interval of the total above zero; P3 positive in at least 5 of the 6 windows; P4 at least 6 pairs positive and the total still above zero without the best pair): NONE.
Trades per year are counted over 2020-04 (or the pair's first candle) to 2025-03, before the confirmation windows.

Supplementary, not part of any rule: the history before the first window (2020-04 to 2022-03), pooled:

| cell | trades | mean net R | total net R [interval] |
|---|---|---|---|
| ema 4h s1 | 558 | +0.643 | +358.9 [+37.0, +785.6] |
| ema 4h s2 | 1027 | +0.279 | +286.3 [+13.3, +620.4] |
| ema 1d s1 | 68 | +14.765 | +1004.0 [-28.3, +3536.9] |
| ema 1d s2 | 131 | +10.976 | +1437.8 [-48.5, +5681.2] |

Nothing passes development. The confirmation windows stay unread and no look is recorded.

Liquidity rule (registered): at least 3 years of 1h candles and a median daily quote volume of at least USD 50 million over the last 365 days.

| pair | years of candles | median daily volume (USD m) |
|---|---|---|
| BTCUSDT | 6.83 | 11477 |
| ETHUSDT | 6.83 | 9109 |
| BNBUSDT | 6.72 | 344 |
| XRPUSDT | 6.82 | 716 |
| SOLUSDT | 6.13 | 1943 |
| DOGEUSDT | 6.31 | 473 |
| SUIUSDT | 3.50 | 212 |
| LINKUSDT | 6.79 | 127 |
| ADAUSDT | 6.75 | 188 |
| LTCUSDT | 6.81 | 76 |
