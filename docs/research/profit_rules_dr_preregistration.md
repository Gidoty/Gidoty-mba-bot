# Pre-registration: the profit rules for Donchian and RSI (sealed test)

Registered before any run. **Plan hash `56e8551ce679ce4a`** (`python -m mba_bot.profit_backtest_dr plan`). It covers the protocol text, pairs, timeframe, both windows, costs, bootstrap settings, every rule set and parameter, the strategies' own parameters and the SHA-256 of every source file the test reads (`profit_backtest_dr.py`, `profit_backtest.py`, `profit_lock.py`, `profit_protect.py`, `ema_exits.py`, `reporting.py`, `shortframe_backtest.py`, `donchian.py`, `rsi_mr.py`, `indicators.py`, `strategy.py`, `research/bootstrap.py`, `research/archive.py`, `research/funding_archive.py`). Data hash `893e32f96ccd0063`.

## Question
Owner decision (3): the same test as PR #96 for `donchian_breakout_v1` and `rsi_mr_v1`. Does the +1R profit lock, or PR #84's take profit, break-even and trail adapted to each strategy, change net R after costs, and is the change distinguishable from noise?

## Windows (which are read and which are not)
- History: 1h candles and funding rates, 2020-01 to 2026-10, BTC, ETH, BNB, XRP USD-M, from the public archive (`data.binance.vision`, no key). UNVERIFIED: that archive klines equal the live API's.
- **In-sample window 2020-04-01 to 2025-01-01 (57 months).** Nothing is tuned on it (the rule sets are fixed here, by the owner's decisions); it is reported as the large sample. Earlier studies (PR #85, PR #96) used 2025-11 onward only, so Donchian and RSI have never been run on it.
- **Confirmation window 2025-01-04 to 2025-11-01 (10 months, 3-day embargo), read once.** No earlier study read any of it. A one-look ledger beside the data records the look BEFORE the window is read; a second look needs `--second-look` and is labelled contaminated.
- **NOT used: 2025-11-01 to 2026-10-31.** The tune and confirmation windows of PR #85 and PR #96 (the EMA study) sit there, and the Donchian and RSI baselines on its first eight months were reported in PR #85. It is excluded so that no number of this study depends on a window that has been looked at. The old confirmation window (2026-07-04 to 2026-09-15) is read (by PR #96, EMA only) and is not used.

## Rule sets (fixed)
- **RS0** the strategy as it runs (its own stop, Donchian trail, channel exit, RSI target, RSI line, time stop, cooldowns).
- **LOCK** the +1R lock: once the best price is 1 R beyond the entry, a stop at the entry plus the cost buffer (both fees at 5 bps, 11 bps round-trip slippage; funding is not added: the test has no running funding total per bar, which makes the lock level slightly optimistic against live by the funding paid before the lock).
- **RS84X** PR #84's rules adapted: Donchian: take profit 3 R (net of costs), break-even stop at +1 R, trail 2 ATR from +1.5 R. RSI: its own profit target kept, break-even stop at +1 R, trail 2 ATR from +1.5 R. R is the distance from the slipped entry to the strategy's initial stop.
- The rules are an overlay: the strategy's own exit is computed exactly as the pre-registered simulations do (a test checks that RS0 reproduces them trade for trade) and a rule stop touched earlier, or in the same candle at a better price, ends the trade first.
- Costs: 5 bps taker per side, 11 bps round-trip slippage, funding at every historical settlement the position was open for. Conventions as PR #96. NOT modelled: 5-minute rounds, the exchange-call limits, real stop fills, the account's real fee tier.

## What is reported, per strategy, rule set and window
Months, trades, average net R with a 95% bootstrap interval, total net R with a month-block bootstrap interval, profit factor, maximum drawdown, longest losing streak, give-back rate, winners cut short, and for each rule set against RS0: the difference per entry (a trade missing on one side counts as zero) with both intervals. **A difference is called distinguishable from noise only if BOTH the per-entry interval and the month-block interval exclude zero.** The per-entry interval treats trades as independent and is too narrow; the month-block one is the one to read. 2000 resamples, seed 20260101.
No parameter is changed after the results. If nothing is distinguishable from noise, that is the answer.
