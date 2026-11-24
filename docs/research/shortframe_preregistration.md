# Short-timeframe research: pre-registration

Registered BEFORE any run on real data. **Plan hash at registration: `d8f9a600f51048b6`.** `python -m mba_bot.shortframe_backtest plan` prints it. The hash covers the pairs, timeframes, strategies, costs, windows, the pass rule, every strategy parameter and the SHA-256 of `shortframe_backtest.py`, `strategy.py`, `donchian.py`, `rsi_mr.py`, `indicators.py` and `research/archive.py`. If any of them changes after tuning, `confirm` refuses. Nothing in this file has been measured on market data.

## Question
Can the bot's three strategies (EMA cross, Donchian breakout, RSI mean reversion), exactly as they are live, make money after real costs on 5-minute, 15-minute and 30-minute candles? The 1-hour candle is the control. The honest answer may be "no" for every cell.

## What is NOT done
No parameter is searched or tuned. The strategies are the live code, unchanged, applied to shorter candles (the indicator periods stay in candles: EMA 20/50, ATR 14, Donchian 20/10, RSI 14). If a cell does not clear the rule, it is reported as not clearing it. There is no second pass with other values.

## Data
Public USD-M Futures candles for BTCUSDT, ETHUSDT, BNBUSDT and XRPUSDT, from Binance's public monthly archive (`data.binance.vision`, no key). The live-API reader in the repo (`research/data.py`, host `fapi.binance.com`) answers HTTP 451 from the build location, so `research/archive.py` (plain GET, one host) writes the same file layout. Intervals 5m, 15m, 30m, 1h. October 2025 is indicator warm-up only.
Windows (UTC, end exclusive): **tune 2025-11-01 to 2026-07-01**; embargo 3 days; **confirm 2026-07-04 to 2026-09-15**. Nothing after 2026-09-15 is read: that stretch was the sealed window of the earlier 1h backtests. Disclosure: the confirm window was TUNING data for the earlier 1h Donchian and RSI backtests (not their sealed months), so for the 1h control cells it is not unseen; for 5m, 15m and 30m it has never been looked at.

## Costs (USD-M Futures)
Taker fee 5 bps per side on both sides, plus the fee guard's assumption of 11 bps round-trip slippage (5.5 bps per side, against the trade on entry and exit). Funding is not modelled. Signal on a closed candle, entry at the NEXT candle's open; when one candle touches the stop and something else, the stop wins; a candle that opens beyond the stop fills at its open. R is the distance from the slipped entry to the stop.

## Rules, as live
EMA: 20/50 crossover with the flat-market filter, stop 2 ATR, exit on the opposite crossover or the stop. Donchian: 20-candle entry channel, volume 1.5x, ATR% limit 3x, stop at the midline or 1.5 ATR, 2 ATR trailing stop, exit on a close beyond the 10-candle opposite channel, 4 hours (real time) after an initial-stop exit on a pair. RSI mean reversion: 14-period, 30/70 lines, ADX below 25, stop 1.5 ATR, target 1 ATR, RSI-50 exit, time stop 24 candles. One position per pair; the four pairs are pooled with NO slot limit, so trade counts are an upper bound on how often the bot could trade. Unfinished trades at a window's end are not counted (reported).

## Differences from live that are named, not hidden
Indicators are computed over the whole series (the live bot uses the latest 200 candles). RSI exits use a 400-candle warm-up slice (the difference from full history is below 1e-6; a test checks it). The Donchian and RSI simulators give the same trades as the existing 1h runners on 1h candles (tests); only the candle length and the costs differ.

## The pass rule (fixed)
A cell is a strategy on a timeframe, pooled over the four pairs, in one window. It clears the rule only if ALL hold: at least 100 trades; net expectancy at least +0.05 R per trade after costs; costs at most 50% of the gross edge (a cell with no gross edge cannot clear it); net-positive on at least 3 of the 4 pairs.
Cells that clear on the tuning window go to the confirmation window, which is read ONCE (a one-look ledger beside the data). A cell passes overall only if it also clears on the confirmation window. If no cell clears on tuning, the confirmation window is not read and the answer is "no".

## Report (per cell)
Trades, trades per day, average holding time, win rate, gross and net expectancy in R, cost per trade in R, the share of the gross edge consumed by costs, worst losing streak, max drawdown at 0.5% risk per trade, pairs net-positive, exits by reason.

## Owner steps (nothing here touches a key, an order or the server)
1. `python -m mba_bot.research.archive download --root research_data --interval 5m --symbols BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT --months 2025-10,...,2026-09` (and 15m, 30m, 1h).
2. `python -m mba_bot.shortframe_backtest plan` and compare with the hash above.
3. `python -m mba_bot.shortframe_backtest tune --root research_data --out research_out/shortframe --expect-plan-hash d8f9a600f51048b6`.
4. Only if a cell qualified: once, `python -m mba_bot.shortframe_backtest confirm --root research_data --out research_out/shortframe --confirm --reason "<why>" --expect-plan-hash d8f9a600f51048b6`.

## Results
Appended below after the runs, with the commit that holds them.
