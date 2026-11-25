# EMA 4h 20/50: the one-look test on the unread months (pre-registration)

Plan hash: `253e2573bcc01497` (`python -m mba_bot.ema_onelook --plan`; SHA-256 of the protocol, window, pairs, rule, costs, data hash and the hashes of every pinned source file). Written and committed BEFORE the run. Research only: no live code path, no switch, no order.

## The one hypothesis
The EMA crossover as shipped (fast 20, slow 50) on 4h candles, with the live bot's stop (2 ATR) and exit (opposite crossover, flat-market filter) exactly as the universe study (#107) simulated them (`htf_study.sim_ema`, unchanged), has a **positive net edge after costs on the 10 pairs of the bot's own catalog**. One cell: no other candle size, parameter set or strategy is evaluated, and nothing is tuned.

## Window and pairs
- Entries from **2025-04-01 00:00 UTC up to and including 2026-09-30** (exclusive end 2026-10-01): 18 calendar months. These are windows 6 to 8 of the #102 geometry, taken as whole calendar months (the geometry's own windows leave out 2025-09-30 and 2026-09-30; they are included here so the six blocks below are whole quarters).
- A trade belongs to the window by its entry time. Trades still open at 2026-10-01 are not counted (the simulator's own rule, as in every earlier study).
- Pairs: BTC, ETH, BNB, XRP, SOL, DOGE, SUI, LINK, ADA, LTC (the bot's catalog, in scan order). Liquidity rule as already used and registered in #107: at least 3.0 years of 1h candles and a median daily quote volume of at least USD 50 million over the last 365 days of the file; the run refuses to start unless exactly these 10 pairs qualify.

## Costs
5 bps taker fee per side, 5.5 bps slippage per side (11 bps round trip), historical funding from the public archive. The BNB reading (fee 10% lower, nothing else changed) is reported separately; **the verdict uses the standard fees**.

## Data
1h candles and funding of the 10 pairs from data.binance.vision (4h candles are built from complete groups of 1h candles). Data hash (`ema_universe.data_hash`, over the 20 files): **`be345d7847ab820d`**. The run refuses to start if the files hash to anything else. The files end on 2026-10-31.

## Pass rule (fixed now; all four must hold for PASS)
- **P1** at least **300** trades in the window. If fewer, the test is declared **UNINFORMATIVE**, not failed, whatever the other parts show.
- **P2** the mean net R per trade is above zero AND the **95%** month-block bootstrap interval of the TOTAL net R has its lower end above zero (2000 resamples of whole calendar months, seed 20261125).
- **P3** the total net R is above zero in at least **4 of the 6** three-month blocks (2025-04 to 06, 07 to 09, 10 to 12, 2026-01 to 03, 04 to 06, 07 to 09; by entry month).
- **P4** the total net R is above zero on at least **6 of the 10** pairs (a pair with no trades does not count as positive).
The verdict is one word: **PASS** (P1 to P4 hold), **FAIL** (P1 holds and any of P2 to P4 does not), **UNINFORMATIVE** (P1 does not hold). It is reported exactly as that, with no softening either way.

## What I expect, so a surprise is visible
The universe study counted 355.8 entries a year over the 10 pairs for 4h 20/50 (2020-04 to 2025-03, per-pair years adjusted for SUI). Over 18 months that is about **534 trades**. A count below 400 or above 700 would be a surprise (a regime far calmer or busier than 2020 to 2025, or a data problem). P1 (300) is therefore expected to hold.

## The look is opened once
- The one-look ledger file (`sealed_looks.ema_onelook.json`, beside the data) is written BEFORE any trade of the window is generated. If any entry exists for this data hash, in any state, the run refuses. There is no flag to reopen.
- If anything crashes after the read starts, the ledger says `crashed` and **the look is spent**: it is reported, not rerun with changes.
- The report is written once, at the end, from one pass.

## What was already known about these months (honest limits of "unread")
- No 4h or 1d trade of this window has been evaluated, printed or counted by any study: #105 and #107 evaluated and counted only entries before 2025-04-01 (their code generates the later trades in memory but filters them out before anything is computed or printed), and the confirmation windows of #105 and #107 were never opened.
- **The same calendar months were read at 1h** on four pairs by the earlier 1h studies (#102 entry study and #103 to #104 cost studies). Market regime knowledge from those readings exists; no 4h result does.
- Pair universe and rule were chosen after seeing the 2020 to 2025 results of #107 (which were not significant). The hypothesis is the one that study left open; this is the first test of it on data outside that period.

## Reported with the result (the rule reads none of these)
Trades, mean and total net R with the interval, gross R, average cost R, per-block and per-pair results, win rate, median trade, the share of the total carried by the 5 best trades, maximum drawdown at 1% risk per trade; the same figures for one slot (first signal wins) and three slots, labelled DESCRIPTIVE ONLY; the BNB reading.

## Limits stated now
One 18-month period (one market regime sequence); candles are built from 1h archive data and not checked against the live API; slippage (11 bps round trip) is an assumption; funding is archive data; the 10 pairs are correlated, so ten pairs are far fewer than ten independent tests.
