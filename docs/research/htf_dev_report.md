# Higher timeframes: development windows (2022-04 to 2025-03)

Plan hash `235575505fe930be`. 4 pairs pooled, own exits, 5 bps taker per side, 11 bps round-trip slippage, historical funding, 99% month-block bootstrap intervals.

| cell | trades | trades per year (all 4 pairs) | per pair per year | avg stop distance | avg gross R | avg cost R | mean net R | total net R [interval] | windows > 0 | P1 | P2 | P3 | passes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ema 4h s1 | 456 | 144.8 | 36.2 | 3.73% | +0.453 | 0.089 | +0.363 | +165.6 [-100.4, +528.2] | 4 | yes | no | no | no |
| ema 4h s2 | 793 | 260.6 | 65.2 | 3.77% | +0.318 | 0.080 | +0.238 | +188.8 [-98.5, +570.4] | 4 | yes | no | no | no |
| ema 1d s1 | 72 | 21.4 | 5.4 | 9.97% | +0.834 | 0.117 | +0.717 | +51.6 [-53.0, +220.7] | 3 | yes | no | no | no |
| ema 1d s2 | 115 | 37.6 | 9.4 | 9.82% | +0.549 | 0.062 | +0.487 | +56.0 [-35.9, +214.6] | 3 | yes | no | no | no |
| donchian 4h s1 | 847 | 279.4 | 69.9 | 2.89% | +0.099 | 0.092 | +0.007 | +6.2 [-87.4, +104.2] | 3 | yes | no | no | no |
| donchian 4h s2 | 490 | 167.2 | 41.8 | 3.02% | +0.169 | 0.090 | +0.079 | +38.9 [-37.5, +119.5] | 5 | yes | no | yes | no |
| donchian 1d s1 | 128 | 42.0 | 10.5 | 7.65% | +0.072 | 0.051 | +0.021 | +2.7 [-43.2, +63.0] | 3 | yes | no | no | no |
| donchian 1d s2 | 80 | 25.2 | 6.3 | 7.83% | +0.167 | 0.057 | +0.110 | +8.8 [-24.9, +54.3] | 3 | yes | no | no | no |
| rsi 4h s1 | 120 | 40.4 | 10.1 | 2.75% | +0.081 | 0.083 | -0.002 | -0.2 [-25.1, +24.2] | 4 | yes | no | no | no |


Cells that pass development (P1 at least 40 trades, P2 mean above zero with the interval of the total above zero, P3 positive in at least 5 of the 6 development windows): NONE.
Trades per year are counted over 2020-04 to 2025-03 (before the confirmation windows).
Nothing passes development. The confirmation windows stay unread and no look is recorded.
