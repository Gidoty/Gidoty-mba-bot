# rsi_mr_v1 backtest: pre-registration

Registered BEFORE any real-data run. Plan hash at registration: `3632d0bc0fb91552`. Nothing in this file has been measured on market data. `python -m mba_bot.rsi_backtest plan` prints the plan hash. The hash covers the arms,
pairs, fees, slippage, windows, selection rule AND the SHA-256 of `src/mba_bot/rsi_mr.py`, so changing the strategy file after tuning makes `confirm` refuse to run.

## Question

Does `rsi_mr_v1`, exactly as specified, have positive expectancy per trade after fees and slippage on the last 6 months? The honest answer may be "no" or "cannot tell".

## Data and windows

* Read-only public 1h candles, the last 6 months, for BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, in the layout the research downloader writes (`klines/1h/<SYMBOL>.csv`). The owner downloads them; this runner reads files only.
* Tune window: the first 4 months. Embargo: 3 days. Sealed window: the last 2 months, read ONCE by `confirm`. Warm-up history before the window is used for the indicators only.
* One set of candles stands in for both markets; only the costs differ (Spot vs Futures). Futures funding is not modelled. Spot and Futures candles differ slightly in reality.

## Costs and rules

Fees per side: Spot 0.10 percent, Futures 0.05 percent. Slippage 0.05 percent per side, against the trade, on every entry and exit. Signal on a closed candle, entry at the next candle's open.
Stop first when one candle touches both. One position per pair at a time. Unfinished trades at the end of a window are not counted (the count is reported).

## Arms (fixed, not widened after seeing results)

SPEC (ADX < 25, stop 1.5 ATR, target 1.0 ATR, time stop 24); ADX20; ADX30; TARGET1.5; STOP2.0. Each changes one thing.

## The one-look ledger

The record that the sealed months were read lives in `sealed_looks.rsi_mr_v1.json` beside the data (in the data folder, not the output folder), keyed on the data hash. A new output folder or deleted output files do not give a clean second look. It is an **honesty aid, not a lock**: anyone who can delete that file can still take a second look, and the report of a second look says it is not unseen evidence.

## Selection rule (fixed)

On the tuning window, Spot costs, all pairs pooled: SPEC stays unless a variant has at least 30 trades, beats SPEC's expectancy by at least 0.05 R and has a max drawdown no more than 10 percent worse.
If SPEC has fewer than 30 trades, SPEC stays. Parameters are not adjusted after the sealed months are read.

## Report (per arm, per market, per pair and pooled)

Trades per week, win rate, average win and loss (R), expectancy per trade after fees (R and percent of notional), max drawdown (equity at 0.5 percent risk per trade), exit reasons, unfinished count.
If the untouched two months are negative after fees, the report says so in plain words. The pooled figure has no slot limit, so it is an upper bound on how often the bot would trade.
Combined trades per week for all three strategies: NOT MEASURED here (this runner measures rsi_mr_v1 only; Donchian does not exist; EMA is measured by the research branch engine).

## Owner steps (the owner runs these; nothing touches the VPS, a key or an order)

1. Download 6 months of 1h candles for the four pairs with the existing downloader (public data, no key).
2. `python -m mba_bot.rsi_backtest plan` and compare the hash with the one in the pull request description.
3. `python -m mba_bot.rsi_backtest tune --root research_data --out research_out/rsi_mr --expect-plan-hash <hash>`; send `tune_report.md`.
4. Once: `python -m mba_bot.rsi_backtest confirm --root research_data --out research_out/rsi_mr --confirm --reason "<why>"`; send `final_report.md`.
