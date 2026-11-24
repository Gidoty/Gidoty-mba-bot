# EMA 4h and 1d as one account: pre-registration (Part 2)

Plan hash: `90e3369f6230d1b5` (`python -m mba_bot.portfolio_study --plan`), committed BEFORE the run. Research only.

Inputs: the trades of the EMA universe study (plan `bc1f9d088852a3c3`), development windows only (entries 2022-04-01 to 2025-03-31). The confirmation windows are not read.

Rules, fixed now:
- Slots: 1, 2, 3 and unlimited. Signals are processed in order of entry time, ties by catalog order (BTC, ETH, BNB, XRP, SOL, DOGE, SUI, LINK, ADA, LTC), which is the scan order Auto Select uses; the first signal takes a free slot. A signal that arrives while every slot is full is dropped, not queued (the live entry window is 10 minutes after the candle closes). A position that ends exactly when a signal arrives has freed its slot. The live bot's tie-break between several EMA pairs is its own score; this study uses scan order instead (stated difference).
- Risk per trade: 1.0% of equity, the bot's default risk percent (also 0.5% and 2.0% in the JSON; 2.0% is the account maximum). Equity changes only when a trade closes (closed-trade equity, compounding in closing order); open trades are not marked to market; a loss beyond the whole account stops at zero.
- Reported per cell and slot rule: signals, taken, dropped, taken per year, total and mean net R with the 99% month-block interval of the total, the total R of the dropped trades, maximum drawdown in R and in equity, lowest and final equity, longest run of losing trades in closing order, the most positions open at once, win rate, median trade, best and worst trade, share of the total from the 5 best trades, months to 100 taken trades.
- No pass rule: this is a description of the same trades, not a test.
