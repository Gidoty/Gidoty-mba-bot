# EMA 4h 20/50, one look: 2025-04-01 to 2026-10-01 (plan hash `253e2573bcc01497`, data hash `be345d7847ab820d`)

## RESULT: FAIL

| part of the pass rule (fixed before the run) | value | holds |
|---|---|---|
| P1 at least 300 trades (fewer = UNINFORMATIVE, not FAIL) | 575 | yes |
| P2 mean net R above zero AND the 95% month-block interval of the total above zero | mean +0.104, total +59.7 [-175.4, +330.2] | no |
| P3 positive in at least 4 of the 6 three-month blocks | 4 of 6 | yes |
| P4 positive on at least 6 of the 10 pairs | 7 of 10 | yes |

Expected before the run: about 534 trades (a count below 400 or above 700 would be a surprise). Observed: 575.

## Numbers (standard fees: 5 bps taker per side, 11 bps round-trip slippage, historical funding)

- trades 575 (383 a year over the 10 pairs); mean net R +0.104 (95% interval of the mean [-0.100, +0.323]); total net R +59.7 [-175.4, +330.2]
- average gross R +0.176; average cost R 0.072; average stop distance 3.99%
- win rate 25%; median trade -1.03 R; best +17.6 R; worst -1.3 R
- the 5 best trades: +74.0 R (124% of the total); total without them -14.3 R
- longest run of losing trades 23; at 1% risk per trade: maximum drawdown 51% of equity (70.1 R), lowest equity 0.76, final equity 1.51 (closed-trade equity, in sample)
- exits: crossover_exit 255, stop_loss 320

### Three-month blocks

| block | trades | total net R | mean net R |
|---|---|---|---|
| 2025-04 to 06 | 92 | +9.0 | +0.097 |
| 2025-07 to 09 | 79 | -7.9 | -0.100 |
| 2025-10 to 12 | 93 | -4.2 | -0.045 |
| 2026-01 to 03 | 123 | +28.0 | +0.228 |
| 2026-04 to 06 | 88 | +18.6 | +0.212 |
| 2026-07 to 09 | 100 | +16.2 | +0.162 |

### Pairs

| pair | trades | total net R | mean net R |
|---|---|---|---|
| BTCUSDT | 55 | +25.4 | +0.461 |
| ETHUSDT | 53 | +8.1 | +0.153 |
| BNBUSDT | 60 | +7.1 | +0.119 |
| XRPUSDT | 53 | +9.7 | +0.182 |
| SOLUSDT | 52 | +25.4 | +0.488 |
| DOGEUSDT | 53 | +9.8 | +0.184 |
| SUIUSDT | 59 | -1.8 | -0.031 |
| LINKUSDT | 56 | +7.2 | +0.128 |
| ADAUSDT | 69 | -12.6 | -0.183 |
| LTCUSDT | 65 | -18.5 | -0.284 |

## Fees paid in BNB (reported separately; the verdict above uses the standard fees)

mean net R +0.107, total +61.4 [-173.8, +331.9], blocks positive 4 of 6, pairs positive 7 of 10 (the same rule would read: FAIL).

## One slot and three slots (DESCRIPTIVE ONLY: not part of the rule; first signal wins, ties by catalog order; closed-trade equity at 1% risk)

| slots | signals | taken | total net R | mean net R | max drawdown (equity) | longest losing streak |
|---|---|---|---|---|---|---|
| 1 slot | 575 | 57 | +14.8 | +0.260 | 9% | 7 |
| 3 slots | 575 | 199 | +8.2 | +0.041 | 29% | 17 |
| unlimited | 575 | 575 | +59.7 | +0.104 | 51% | 23 |
