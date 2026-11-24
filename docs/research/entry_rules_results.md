# Entry-rule study: results

Plan hash `6e9a567b49a39786` (registered in commit `ebbe02b` before the run, run once). Data hash `893e32f96ccd0063`. 1h candles, BTC, ETH, BNB, XRP, nine walk-forward test windows of six months from 2022-04 to 2026-09, each strategy with its own exits, 5 bps taker per side, 11 bps round-trip slippage, historical funding, 99% month-block bootstrap intervals. The full table (every candidate, every strategy) is in `docs/research/entry_rules_table.md`. **A measurement, not evidence of an edge.**

## The answer
**No candidate rule passed. None even met P1 to P3 ("an edge that survives costs and the windows"), for any of the three strategies.** Of the 31 candidate rows (filters, guards and pair choices; the pre-registration said "about 40", the exact count is 31) and the 3 walk-forward picks, none had a positive net R per trade whose interval excluded zero, none was positive in 7 of the 9 windows, and none beat its unfiltered strategy by an amount distinguishable from noise. One row, `fee3` for EMA, was distinguishably NEGATIVE on its total.

| strategy (unfiltered) | trades | mean net R | total net R [99% month-block interval] | test windows with a positive total (of 9) |
|---|---|---|---|---|
| EMA crossover | 2,659 | -0.123 | -327.5 [-757.2, +155.5] | 4 |
| Donchian breakout | 5,296 | -0.174 | -921.5 [-1,208.3, -617.7] | 0 |
| RSI mean reversion | 624 | -0.285 | -178.1 [-248.9, -112.9] | 0 |

- **Donchian and RSI lose money after costs and funding, and the loss is distinguishable from noise: in all nine windows for both.** No filter or guard changed that: the best Donchian row (higher-timeframe trend, `htf4`) was -0.157 R per trade (a difference of +0.017 [-0.038, +0.067] from unfiltered); the best RSI row was `fee3` at -0.255.
- **EMA is not distinguishable from zero** (total interval -757 to +155; 4 of 9 windows positive) but its point estimate is negative. The best filter, `vol` (signal volume at least the 20-candle average), was -0.064 R per trade, +0.059 better than unfiltered with an interval of -0.068 to +0.207: indistinguishable. Pair choice did not help (`pair:ETHUSDT` -0.006, 3 of 9 windows positive; the trailing-best-pair rule -0.152).
- **The walk-forward pick (the best candidate on each tune window, traded in the next window) lost in every strategy:** EMA -0.189 R per trade (unfiltered -0.123), Donchian -0.183 (-0.174), RSI -0.257 (-0.285; +0.028 better, interval -0.044 to +0.092, so not distinguishable). Choosing a rule from the past did not find a rule that held.
- **Existing strategy-guard rules** (the fee guard `fee3`, the cooldown `cool24`, the loss-streak pause `streak3`) changed results by amounts within noise; `fee3` removes 26% to 32% of trades and made EMA worse.
- **EMA `gap` (minimum |fast - slow| of 0.25 ATR at the crossover) left 8 trades**: at the candle where the averages cross their gap is near zero by construction, so the shipped guard at this value refuses almost every entry. It is not informative at 0.25 and I did not try other values (that would be tuning).

## What this means for the three strategies as they run live today
- **Measured: none of the three has a demonstrated positive net edge on 1h after costs.** Donchian and RSI have a demonstrated negative one (about -0.17 R and -0.29 R per trade over 2022 to 2026; -0.17 and -0.27 R in the 57 months to 2024 and -0.17 and -0.22 R in the 10 months of 2025 from the previous study). EMA is about -0.12 R per trade with an interval that includes zero.
- Per trade this means that, on these data, the expected result of running them is a loss of roughly 0.1 to 0.3 of the amount risked per trade before any other cost of operating the service. A short pilot of a few dozen trades cannot show a different sign reliably (single-trade R has a standard deviation above 1), so a good or bad pilot month is not evidence either way.
- No entry filter, guard, higher-timeframe condition or pair choice in this list changes that. The exit rules (the +1R lock, take profit and trail) did not either (PRs #96 and the Donchian/RSI study).
- Not tested here, and so still open: other entry ideas than these candidates, other timeframes (the engine stays on 1h by decision), other assets, parameter changes of the strategies themselves, portfolio or sizing effects.

## Limits
- Costs assumed: 5 bps taker per side (the standard rate; the account's real tier is not known here), 11 bps slippage. Better fills would shift every number up by the same small amount and would not change the sign of Donchian or RSI.
- 31 candidates at the 1% level: a pass by chance would have been expected about 0.3 times. There were none.
- The data span includes periods looked at by earlier exit studies. No entry rule had been evaluated before, and none was tuned. Archive candles and funding are UNVERIFIED against the live API.
- Intervals are month-block bootstrap intervals and can still be too narrow, because market regimes last longer than a month.
