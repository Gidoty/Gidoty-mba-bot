# Cost split: where the loss comes from

Plan hash `fae0101feb37af11`. The same trades as PRs #101 and #102 (1h, BTC, ETH, BNB, XRP pooled), each trade's net R taken apart in R (R = distance from the entry to the initial stop): gross (before any cost) minus taker fees (5 bps per side), slippage (5.5 bps per side, 11 round trip) and funding. The identity net = gross - fee - slippage - funding holds trade by trade (largest error shown).

| strategy | window | trades | gross R | fees R | slippage R | funding R | net R | costs as share of the loss | signal as share of the loss | largest identity error |
|---|---|---|---|---|---|---|---|---|---|---|
| ema | A: 57 months (2020-04 to 2024-12) | 2725 | +0.205 [+0.084, +0.339] | -0.060 | -0.066 | -0.021 | +0.058 | n/a% | n/a% | 5.5e-14 |
| ema | B: 10 months (2025-01 to 2025-10) | 493 | -0.157 [-0.328, +0.029] | -0.067 | -0.073 | -0.004 | -0.300 | 48% | 52% | 3.4e-14 |
| ema | C: the nine test windows of #102 (2022-04 to 2026-09) | 2659 | +0.028 [-0.079, +0.140] | -0.070 | -0.076 | -0.005 | -0.123 | 123% | -23% | 5.5e-14 |
| donchian | A: 57 months (2020-04 to 2024-12) | 5550 | -0.005 [-0.037, +0.030] | -0.078 | -0.086 | -0.003 | -0.173 | 97% | 3% | 8.1e-14 |
| donchian | B: 10 months (2025-01 to 2025-10) | 1012 | +0.032 [-0.053, +0.117] | -0.094 | -0.103 | -0.002 | -0.166 | 120% | -20% | 7.1e-14 |
| donchian | C: the nine test windows of #102 (2022-04 to 2026-09) | 5296 | +0.027 [-0.010, +0.063] | -0.095 | -0.104 | -0.002 | -0.174 | 116% | -16% | 1.0e-13 |
| rsi | A: 57 months (2020-04 to 2024-12) | 655 | -0.109 [-0.172, -0.043] | -0.076 | -0.084 | +0.003 | -0.266 | 59% | 41% | 4.2e-14 |
| rsi | B: 10 months (2025-01 to 2025-10) | 113 | -0.025 [-0.178, +0.119] | -0.091 | -0.101 | +0.001 | -0.215 | 88% | 12% | 5.9e-14 |
| rsi | C: the nine test windows of #102 (2022-04 to 2026-09) | 624 | -0.090 [-0.155, -0.023] | -0.094 | -0.103 | +0.002 | -0.285 | 68% | 32% | 5.9e-14 |

Gross, fees, slippage and funding are shown with the sign they have in net R (a cost is negative). 'Costs as share of the loss' is (fees + slippage + funding) / (-net) and 'signal as share' is (-gross) / (-net): when gross is positive the signal share is negative and the costs are more than the whole loss.

## Exit reasons (share of trades)

| strategy | window | reasons (count, share) |
|---|---|---|
| ema | A: 57 months (2020-04 to 2024-12) | stop_loss 1590 (58%), crossover_exit 1135 (42%) |
| ema | B: 10 months (2025-01 to 2025-10) | stop_loss 314 (64%), crossover_exit 179 (36%) |
| ema | C: the nine test windows of #102 (2022-04 to 2026-09) | stop_loss 1629 (61%), crossover_exit 1030 (39%) |
| donchian | A: 57 months (2020-04 to 2024-12) | trailing_stop 4165 (75%), stop_loss 1379 (25%), channel_exit 6 (0%) |
| donchian | B: 10 months (2025-01 to 2025-10) | trailing_stop 721 (71%), stop_loss 283 (28%), channel_exit 8 (1%) |
| donchian | C: the nine test windows of #102 (2022-04 to 2026-09) | trailing_stop 3882 (73%), stop_loss 1398 (26%), channel_exit 16 (0%) |
| rsi | A: 57 months (2020-04 to 2024-12) | profit_target 323 (49%), stop_loss 316 (48%), rsi_exit 14 (2%), time_stop 2 (0%) |
| rsi | B: 10 months (2025-01 to 2025-10) | profit_target 61 (54%), stop_loss 48 (42%), time_stop 3 (3%), rsi_exit 1 (1%) |
| rsi | C: the nine test windows of #102 (2022-04 to 2026-09) | profit_target 310 (50%), stop_loss 295 (47%), rsi_exit 13 (2%), time_stop 6 (1%) |

## Stop distance and cost per pair (window A, the 57 months)

| strategy | pair | trades | stop distance (% of price) | gross R | fees R | slippage R | funding R | net R |
|---|---|---|---|---|---|---|---|---|
| ema | BTCUSDT | 678 | 1.79% | +0.075 | -0.069 | -0.076 | -0.015 | -0.085 |
| ema | ETHUSDT | 661 | 2.15% | +0.403 | -0.058 | -0.063 | -0.033 | +0.250 |
| ema | BNBUSDT | 685 | 2.14% | +0.237 | -0.062 | -0.068 | -0.020 | +0.088 |
| ema | XRPUSDT | 701 | 2.55% | +0.110 | -0.051 | -0.056 | -0.017 | -0.014 |
| donchian | BTCUSDT | 1422 | 1.35% | +0.033 | -0.095 | -0.104 | -0.002 | -0.168 |
| donchian | ETHUSDT | 1461 | 1.71% | -0.001 | -0.074 | -0.081 | -0.004 | -0.161 |
| donchian | BNBUSDT | 1379 | 1.70% | -0.027 | -0.079 | -0.087 | -0.003 | -0.197 |
| donchian | XRPUSDT | 1288 | 2.08% | -0.028 | -0.064 | -0.071 | -0.003 | -0.166 |
| rsi | BTCUSDT | 148 | 1.28% | -0.208 | -0.094 | -0.104 | +0.003 | -0.404 |
| rsi | ETHUSDT | 167 | 1.66% | -0.080 | -0.076 | -0.083 | +0.004 | -0.235 |
| rsi | BNBUSDT | 170 | 1.70% | -0.049 | -0.077 | -0.085 | +0.003 | -0.207 |
| rsi | XRPUSDT | 170 | 2.25% | -0.112 | -0.060 | -0.066 | +0.002 | -0.235 |
