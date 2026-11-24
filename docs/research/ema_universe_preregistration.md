# EMA on 4h and 1d across the pair catalog: pre-registration

Plan hash: `bc1f9d088852a3c3` (SHA-256 of the protocol, the universe, the rule, the costs and the hashes of the pinned source files; `python -m mba_bot.ema_universe plan`). Written and committed BEFORE the development run. Research only: no live code path, no switch, no order.

## Question
EMA trend following on 4h and 1d candles had positive point estimates on four pairs that could not be told from zero (#105). Does it show an edge across the bot's whole catalog, with nothing tuned?

## Candidates (nothing tuned)
EMA crossover as shipped, on 4h and on 1d: s1 = 20/50 (the live parameters) and s2 = 12/26 (one classic pair). Four cells. Stop 2 ATR(14) from the entry, exit on the opposite crossover, flat-market filter, closed-candle signal and next-open entry, exactly the simulator of #105 (`htf_study.sim_ema`, which reproduces the entry study's 1h simulator trade for trade). 4h and daily candles are built from complete groups of 1h candles.

## Universe and liquidity rule (fixed before the run)
The bot's own catalog in scan order: BTC, ETH, BNB, XRP (supported), SOL, DOGE, SUI, LINK, ADA, LTC (futures extras): 10 pairs. A pair qualifies when the public archive holds at least 3.0 years of 1h candles AND its median daily quote volume over the last 365 days of the file (to 2026-10-31) is at least USD 50 million. The run refuses to start unless exactly these 10 pairs qualify. **The catalog has 10 pairs, not 12; I did not add pairs from outside it.** I looked at the volume figures before fixing the USD 50 million line (lowest: LTC 76 million, LINK 127 million); the line was set to a round number below every pair, so it is a statement, not a filter. SUI has 3.5 years, so its first windows have no trades.

## Costs
5 bps taker per side, 5.5 bps slippage per side (11 round trip), historical funding from the archive. Second reading: fees paid in BNB (10% off the fee, nothing else changed; `cost_sensitivity.recost`). The pass rule uses the standard fees; the BNB reading is reported.

## Windows
The nine 182-day windows of the entry study (2022-04 to 2026-09). Development: windows 0 to 5 (2022-04 to 2025-03). Confirmation: windows 6 to 8 (2025-04 to 2026-09), **unread**: #105 found nothing and never opened them, so no 4h or 1d trade from these windows has been evaluated or counted. (The 1h entry study did read these calendar months for 1h on four pairs; the months are not fresh in that sense.) A trade belongs to the window of its entry time.

## Development pass rule (a cell must meet all four)
- P1 at least 150 trades pooled over the 10 pairs.
- P2 mean net R above zero AND the 99% month-block bootstrap interval of the total net R above zero (reps 2000, seed 20260505).
- P3 total net R above zero in at least 5 of the 6 windows (a window with no trades is not positive).
- P4 not one pair: at least 6 of the pairs with trades have a positive total, and the total stays above zero when the best pair is left out.
Four cells are tested, so the 99% level is the guard against picking a lucky one.

## Confirmation (only cells that pass development; read once)
The one-look ledger is written before the windows are read. Rule: at least 60 trades, mean net R above zero, total above zero, positive in at least 2 of 3 windows. A second look needs a flag and is labelled contaminated. If no cell passes, the confirmation windows stay unread and no look is recorded.

## Reported with the result (no rule reads them)
Per pair totals; trades per year per pair and for the universe over 2020-04 (or the pair's first candle) to 2025-03 (counts only, no confirmation-window trade); the positions that would be wanted at once if every signal were taken (time average, share of time with 1 or more and 2 or more, maximum) over 2023-09-01 to 2025-04-01, when all 10 pairs are active; months to 100 trades on 1 pair (average, slowest, fastest), the four original pairs and the universe; the history before the first window (2020-04 to 2022-03), pooled, labelled supplementary.

## Limits stated now
Candles, funding and the 11 bps slippage are assumptions or archive data, not measured on the account; every trade is a simulation. Pairs are correlated, so ten pairs are fewer than ten independent tests. The windows overlap market regimes already seen at 1h.
