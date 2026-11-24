# Pre-registration: EMA, Donchian and RSI on higher timeframes

Registered before any run. **Plan hash `235575505fe930be`** (`python -m mba_bot.htf_study plan`). Data hash `893e32f96ccd0063`. Covers the cells, parameters, windows, rules, bootstrap settings, costs and the SHA-256 of every pinned source.

## Question
The cost of a trade in R is (fees + slippage + funding) / stop distance; on 1h it is about 0.15 to 0.20 R per trade (cost split). The stop distance in percent grows with the candle length. Does any of EMA and Donchian on 4h and 1d, or RSI on 4h, keep a positive net edge after costs and is it stable across time? A new, separate question from the 1h studies: nothing was ever run on these timeframes.

## Cells (9, parameters fixed here, nothing tuned)
EMA on 4h and 1d with fast/slow periods 20/50 (shipped) or 12/26 (a classic alternative); Donchian on 4h and 1d with entry/exit channel 20/10 (shipped) or 55/20 (the classic turtle values); RSI on 4h as shipped. All other parameters as shipped (ATR 14, EMA flat filter, Donchian volume and ATR filters, trail 2 ATR, RSI 30/70, ADX at most 25, time stop 24 candles). 4h and daily candles are built from complete groups of 1h candles (exactly how the exchange builds them). Same four pairs, same 2020-04 to 2026-09 history, same costs (5 bps taker per side, 11 bps round-trip slippage, historical funding), own exits, no profit rules.

## Windows and the one look
The nine 182-day windows of the entry study. **Development windows 1 to 6 (2022-04-01 to 2025-03-30)** are reported first. **Confirmation windows 7 to 9 (2025-04-01 to 2026-09-29)** are opened ONCE, only for cells that passed development, with a ledger written before they are read; if no cell passes, they stay unread and nothing is recorded. The development run neither counts nor evaluates a single trade from the confirmation windows (a test checks this); trades per year are counted over 2020-04 to 2025-03 only. Earlier studies never ran these timeframes, so no confirmation window has been read for them. The same calendar months were read for 1h strategies.

## Rules (fixed)
Development, on the trades of the six development windows pooled: **P1** at least 40 trades; **P2** mean net R above zero AND the 99% month-block interval of the pooled total above zero; **P3** net total R above zero in at least 5 of the 6 windows (a window with no trades is not positive). Confirmation, on the three confirmation windows: at least 15 trades, mean net R above zero, positive in at least 2 of 3 windows. Also reported for every cell: trades per year (all pairs and per pair), average stop distance in percent, average gross R and average cost in R. 9 cells at 1%: about 0.09 false passes expected.
