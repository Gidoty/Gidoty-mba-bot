# donchian_breakout_v1 backtest: pre-registration

Registered BEFORE any real-data run. Plan hash at registration: `86ba3b8a928c6112`. Nothing in this file has been measured on market data. `python -m mba_bot.donchian_backtest plan` prints the plan hash. The hash covers the arms, pairs, fees,
slippage, cooldown, windows, selection rule AND the SHA-256 of `src/mba_bot/donchian.py`, so changing the strategy file after tuning makes `confirm` refuse to run.

## Question

Does `donchian_breakout_v1`, exactly as specified, have positive expectancy per trade after fees and slippage on the last 6 months? The honest answer may be "no" or "cannot tell". A breakout
strategy can lose small amounts often and win rarely; two months may contain no large winner.

## Data and windows

* Read-only public 1h candles, the last 6 months, for BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, in the layout the research downloader writes (`klines/1h/<SYMBOL>.csv`). **Volume is used** (the volume confirmation reads the `volume` column).
* Tune window: the first 4 months. Embargo: 3 days. Sealed window: the last 2 months, read ONCE by `confirm`. Warm-up history before the window is used for the indicators only.
* One set of candles stands in for both markets; only the costs differ (Spot vs Futures). Futures funding is not modelled.

## Costs and rules

Fees per side: Spot 0.10 percent, Futures 0.05 percent. Slippage 0.05 percent per side, against the trade, on every entry and exit. Signal on a closed candle, entry at the next candle's open.
Initial stop = the channel midline or 1.5 ATR from the entry fill, whichever is closer. Exit: stop, 2 ATR trailing stop, or a close beyond the opposite 10-candle channel (the same functions the live path uses).
One position per pair at a time. After an initial-stop exit on a pair, no new signal on that pair for 4 hours. Unfinished trades at the end of a window are not counted (the count is reported).

## Arms (fixed, not widened after seeing results)

SPEC (20-candle entry channel, 10-candle exit channel, volume 1.5x, ATR% limit 3x, trail 2 ATR); VOL1.2; VOL2.0; ENTRY30 (30-candle entry channel and volume window); TRAIL3.0. Each changes one thing.

## Selection rule (fixed)

On the tuning window, Spot costs, all pairs pooled: SPEC stays unless a variant has at least 30 trades, beats SPEC's expectancy by at least 0.05 R and has a max drawdown no more than 10 percent worse.
If SPEC has fewer than 30 trades, SPEC stays. Parameters are not adjusted after the sealed months are read. Breakouts are rare, so SPEC may well have fewer than 30 trades on 4 months; then SPEC is reported as is.

## Report (per arm, per market, per pair and pooled)

Trades per week, win rate, average win and loss (R), expectancy per trade after fees (R and percent of notional), max drawdown (equity at 0.5 percent risk per trade), exit reasons, unfinished count.
If the untouched two months are negative after fees, the report says so in plain words. The pooled figure has no slot limit, so it is an upper bound on how often the bot would trade.
**Combined trades per week for all three strategies: NOT MEASURED.** The three runners are separate (this one measures donchian_breakout_v1; `rsi_backtest` measures rsi_mr_v1; the EMA strategy is
measured by the research branch engine) and their outputs are not combined into a measured figure.

## Owner steps (the owner runs these; nothing touches the VPS, a key or an order)

1. Download 6 months of 1h candles for the four pairs with the existing downloader (public data, no key). The same files serve `rsi_backtest`.
2. `python -m mba_bot.donchian_backtest plan` and compare the hash with the one in the pull request description.
3. `python -m mba_bot.donchian_backtest tune --root research_data --out research_out/donchian --expect-plan-hash <hash>`; send `tune_report.md`.
4. Once: `python -m mba_bot.donchian_backtest confirm --root research_data --out research_out/donchian --confirm --reason "<why>"`; send `final_report.md`.
