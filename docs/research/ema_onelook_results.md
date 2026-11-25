# EMA 4h 20/50, one look on 2025-04-01 to 2026-10-01: result

Plan hash `253e2573bcc01497` (committed before the run, with the data hash `be345d7847ab820d`). The ledger (`ema_onelook_ledger.json`, copied from beside the data) says the window was opened once, at 2026-11-25 15:39:58 UTC, and the run **completed** (it did not crash). The full tables are in `ema_onelook_report.md` and `ema_onelook_results.json`; nothing was changed or re-run after the read.

## Result against the pass rule: FAIL

| part | rule | observed | holds |
|---|---|---|---|
| P1 | at least 300 trades | 575 | yes |
| P2 | mean net R above zero AND the 95% month-block interval of the total above zero | mean +0.104 R; total +59.7 R, interval **[-175.4, +330.2]** | **no** |
| P3 | positive in at least 4 of 6 three-month blocks | 4 of 6 | yes |
| P4 | positive on at least 6 of 10 pairs | 7 of 10 | yes |

All four had to hold. P2 did not: the lower end of the interval of the total is below zero. The verdict is FAIL (P1 held, so this is not "uninformative"). The BNB reading (+0.107 R mean, interval [-173.8, +331.9]) gives the same verdict.

## Expected against observed
Expected about 534 trades (surprise below 400 or above 700): observed 575. No surprise in the count.

## The numbers
- 575 trades (383 a year over the 10 pairs); mean net R +0.104; total net R +59.7. Average gross R +0.176, average cost R 0.072 (fees, slippage, funding), average stop distance 3.99%.
- Win rate 25%; median trade -1.03 R; best +17.6 R; worst -1.3 R. Exits: 320 stop-losses, 255 crossover exits.
- The 5 best trades add up to +74.0 R, which is 124% of the total: without them the total is -14.3 R.
- Longest run of losing trades 23. At 1% risk per trade (closed-trade equity, in sample): maximum drawdown 51% of equity (70.1 R), lowest equity 0.76, final equity 1.51.
- Blocks (total net R): +9.0, -7.9, -4.2, +28.0, +18.6, +16.2. Pairs: positive on BTC +25.4, SOL +25.4, DOGE +9.8, XRP +9.7, ETH +8.1, LINK +7.2, BNB +7.1; negative on SUI -1.8, ADA -12.6, LTC -18.5.
- Descriptive only (not part of the rule): one slot, first signal wins: 57 trades taken of 575, +14.8 R, maximum drawdown 9%, longest losing run 7. Three slots: 199 taken, +8.2 R, maximum drawdown 29%, longest losing run 17. Unlimited: the figures above.

## What this does and does not say
- The point estimate of the mean is positive (+0.104 R) and three of the four parts held. The interval of the total runs from -175 R to +330 R, so the data do not separate this result from zero; that is what P2 tested.
- The average gross edge before costs (+0.176 R) is larger than the average cost (0.072 R), but a handful of trades carry the total.
- The look is spent. These 18 months are no longer unread, and per the pre-registered consequence nothing is tuned on this window afterwards.

## Where I am unsure
- The window is whole calendar months, which adds 2025-09-30 and 2026-09-30 to the #102 windows 6 to 8 (declared before the run).
- P3 held at exactly the threshold (4 of 6 blocks): a different split of the same trades into blocks could read differently; the blocks were fixed in advance.
- Entries are counted by entry time and trades still open on 2026-10-01 are not counted (the simulator's rule), so the last days of the window are slightly under-counted.
- The interval resamples whole calendar months; with only 18 months it is wide by construction.
- The same months were read at 1h on four pairs by earlier studies; no 4h result for them existed.
