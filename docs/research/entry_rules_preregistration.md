# Pre-registration: the entry-rule study

Registered before any run. **Plan hash `6e9a567b49a39786`** (`python -m mba_bot.entry_study plan`). Data hash `893e32f96ccd0063`. The hash covers the protocol, windows, every candidate rule and its parameter, the pass rule, the bootstrap settings, costs and the SHA-256 of every source file read (`entry_study.py`, `profit_backtest_dr.py`, `profit_backtest.py`, `shortframe_backtest.py`, `donchian.py`, `rsi_mr.py`, `indicators.py`, `strategy.py`, `reporting.py`, `research/bootstrap.py`, `research/archive.py`, `research/funding_archive.py`). One run; nothing is changed after it.

## Question
Owner decision (4): does any entry rule have a positive net edge after costs on 1h candles, and is it stable across time? What does the answer mean for the three strategies as they run today?

## Data and what has been seen
1h candles and funding, BTC, ETH, BNB, XRP USD-M, 2020-04 to 2026-09 (public archive, no key; UNVERIFIED that it equals the live API). Earlier studies looked at EXIT rules on parts of this history (2025-11 onward by PRs #85 and #96; 2020-04 to 2025-10 by the Donchian and RSI profit-rule study). **No entry rule has ever been evaluated on any of it.** There is no untouched window in this study; the safeguards are a fixed candidate list, a pass rule fixed now, nine separate test windows and wide (99%) intervals.

## Design: walk-forward, nine windows
Test windows of 182 days starting 2022-04-01, 2022-10-01, 2023-04-01, 2023-10-01, 2024-04-01, 2024-10-01, 2025-04-01, 2025-10-01, 2026-04-01 (none overlaps the next). The tune window of each is the 730 days before it. Every strategy is simulated once per candidate over the whole period with its own exits (the strategies as they run: no profit rules), and a trade belongs to the window it was ENTERED in. Costs: 5 bps taker per side, 11 bps round-trip slippage, historical funding (as in the other studies). Nothing is fitted: every rule has one fixed parameter, registered here.

## Candidates (fixed)
- **EMA** (EMA 20/50 crossover, 2 ATR stop, opposite-crossover exit): `gap` the crossover's |fast - slow| at least 0.25 ATR (the existing guard); `htf4` the 4h EMA20 above/below EMA50 in the trade's direction; `htfD` the same on daily bars; `adx` ADX(14) at least 20; `vol` the signal candle's volume at least the previous 20-candle average; `volat` ATR% at least its median over the previous 500 candles; `cool24` no entry within 24 hours after a plain stop-out; `streak3` no entry for 24 hours after 3 consecutive losing trades; `fee3` the existing fee guard: ATR% at least 3 x the 21 bps round-trip cost.
- **Donchian** (its own volume, ATR and cooldown filters stay; these are ADDED): `htf4`, `cool24` (24 hours instead of the shipped 4), `streak3`, `fee3`.
- **RSI** (its own ADX filter stays): `htfD` daily trend aligned with the trade, `streak3`, `fee3`.
- **Choice of pair**, for each strategy: each pair alone, and "trailing best": at each test-window start the pair with the best net total R over the previous 365 days (counting only trades already exited).
- **The walk-forward pick**: on each tune window the candidate (including none) with the highest mean net R among those with at least 100 tune trades, traded in the following test window.
Higher-timeframe bars are used only once complete (a test checks there is no look-ahead).

## Pass rule (fixed)
A candidate passes only if ALL hold, on the trades of the nine test windows pooled: **P1** at least 300 trades; **P2** mean net R per trade above zero AND the 99% month-block bootstrap interval of the pooled total net R above zero; **P3** net total R above zero in at least 7 of the 9 windows; **P4** its mean net R per trade exceeds the unfiltered strategy's, with the 99% month-block interval of the difference above zero. P1 to P3 together are "an edge that survives costs and the windows"; P4 says whether the RULE is what created it. About 40 candidates are tested at the 1% level, so about 0.4 false passes are expected by chance; a lone pass proves little. If nothing passes, that is the answer and no rule is tuned.
