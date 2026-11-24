# Cost split: results

Plan hash `fae0101feb37af11` (registered in commit `b0c7922` before the run). The same trades as PRs #101 and #102, 1h, BTC, ETH, BNB, XRP pooled; EMA added on #101's two windows. R = distance from the entry to the initial stop. Costs as in those studies: 5 bps taker per side, 5.5 bps slippage per side (11 round trip), historical funding. The identity net = gross - fees - slippage - funding holds on every trade (largest error 1e-13). A description of trades already measured, not a new test.

## Per trade, in R (A = 57 months to 2024-12, B = 10 months of 2025, C = #102's nine test windows)
| strategy | window | trades | gross (before any cost) [95% CI] | fees | slippage | funding | net |
|---|---|---|---|---|---|---|---|
| EMA | A | 2,725 | +0.205 [+0.084, +0.339] | -0.060 | -0.066 | -0.021 | **+0.058** |
| EMA | B | 493 | -0.157 [-0.328, +0.029] | -0.067 | -0.073 | -0.004 | -0.300 |
| EMA | C | 2,659 | +0.028 [-0.079, +0.140] | -0.070 | -0.076 | -0.005 | -0.123 |
| Donchian | A | 5,550 | -0.005 [-0.037, +0.030] | -0.078 | -0.086 | -0.003 | -0.173 |
| Donchian | B | 1,012 | +0.032 [-0.053, +0.117] | -0.094 | -0.103 | -0.002 | -0.166 |
| Donchian | C | 5,296 | +0.027 [-0.010, +0.063] | -0.095 | -0.104 | -0.002 | -0.174 |
| RSI | A | 655 | -0.109 [-0.172, -0.043] | -0.076 | -0.084 | +0.003 | -0.266 |
| RSI | B | 113 | -0.025 [-0.178, +0.119] | -0.091 | -0.101 | +0.001 | -0.215 |
| RSI | C | 624 | -0.090 [-0.155, -0.023] | -0.094 | -0.104 | +0.002 | -0.285 |

## What share of the loss is cost and what share is the signal
- **Donchian: the loss is the cost.** Before any cost the signal earns about nothing (-0.005 and +0.027 R per trade, intervals around zero). Fees, slippage and funding take 0.17 to 0.20 R per trade, which is 97% to 120% of the loss. The signal share is about 0% (+3% in A, and negative in B and C, where gross is slightly positive).
- **RSI: part cost, part signal.** The signal itself loses before costs in the large windows (-0.109 R in A, interval -0.172 to -0.043; -0.090 in C, -0.155 to -0.023), which is 41% and 32% of the loss; costs are 59% and 68%. In the 10-month window the gross is about zero (-0.025) and costs are 88%.
- **EMA: the signal has a gross edge in the early years and the costs take it.** Gross +0.205 R in A (distinguishable from zero) against 0.147 R of costs leaves +0.058 net (the only positive net of the nine rows). In 2025 (B) the gross itself was negative (-0.157). Over #102's test windows gross is +0.028 and costs are 0.151, so costs are 123% of the loss.
- **Costs are about 0.13 to 0.20 R per trade** depending on the strategy and window, with slippage (an assumption: 11 bps round trip) the largest part, fees next, funding small (0.00 to 0.02 R; the one that matters for EMA's long holds).

## The cost in R depends on the stop distance
Average stop distance as a percent of price (window A): BTC 1.28% to 1.79%, ETH 1.66% to 2.15%, BNB 1.70% to 2.14%, XRP 2.08% to 2.55% (ranges across the three strategies; Donchian 1.35%, 1.71%, 1.70%, 2.08%; RSI 1.28%, 1.66%, 1.70%, 2.25%; EMA 1.79%, 2.15%, 2.14%, 2.55%). The tighter the stop, the larger the cost in R: Donchian on BTC (stop 1.35%) pays 0.199 R in fees and slippage, on XRP (2.08%) 0.135 R. Per pair, EMA net R in A was BTC -0.085, ETH +0.250, BNB +0.088, XRP -0.014: gross edge differed far more than cost did.

## How often each exit fires
| strategy | window A | 
|---|---|
| EMA | stop 58%, opposite crossover 42% |
| Donchian | trailing stop 75%, initial stop 25%, channel exit 0.1% |
| RSI | profit target 49%, stop 48%, RSI line 2%, time stop 0.3% |
(Windows B and C are within 5 points of these.) Donchian's 10-candle channel exit almost never fires: its 2 ATR trail is hit first. A time stop never matters for RSI.

## Limits
Slippage (11 bps round trip) and the 5 bps fee are assumptions of the earlier studies, not measurements of this account; the exchange-side stop's slippage is the least certain. Intervals are iid over trades (too narrow). Archive data UNVERIFIED against the live API.
