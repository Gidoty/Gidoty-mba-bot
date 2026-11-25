# EMA 4h 20/50 on the 18 months nobody had read: one page for the owner

Facts and limits only.

## The result
**FAIL.** The pre-registered test (one hypothesis: the shipped EMA 20/50 on 4h candles, with the bot's own stop and exit, has a positive net edge after costs on the 10 catalog pairs; window 2025-04-01 to 2026-10-01; opened once) did not pass.

| rule (set before the run) | observed |
|---|---|
| P1 at least 300 trades | 575 (expected about 534): holds |
| P2 mean above zero and the 95% interval of the total above zero | mean +0.104 R per trade; total +59.7 R, interval -175 to +330 R: **does not hold** |
| P3 positive in 4 of 6 three-month blocks | 4 of 6: holds |
| P4 positive on 6 of 10 pairs | 7 of 10: holds |

All four were needed. The other numbers: 25% of trades win, the median trade loses 1.03 R, the five best trades make 124% of the total (without them the total is -14.3 R), the longest losing run is 23 trades, and at 1% risk per trade the equity fell 51% from peak at its worst. With one slot (first signal wins) the same trades gave 57 trades and +14.8 R, three slots 199 trades and +8.2 R; these are descriptions, not part of the rule.

## What each outcome meant, and which one this is
- PASS would have meant EMA 4h is not rejected and moves to a small live forward test; it would not have been proof of profit.
- **FAIL (this result) means the shipped 4h setting is not supported by the data, and nothing is tuned on this window afterwards.**
- UNINFORMATIVE (fewer than 300 trades) would have meant the test says nothing either way. It did not happen.

For the bot: on this evidence EMA 4h 20/50 does not move to forward testing. Nothing in the bot changes because of this document.

## Limits
- One 18-month period: one sequence of market conditions.
- The 4h candles are built from 1h archive data and were not checked against the live Binance API; funding is archive data.
- Slippage (11 bps round trip) is an assumption, and stop fills in fast markets were not measured.
- The 10 pairs move together, so they are far fewer than ten independent tests.
- The test could not separate the result from zero: the interval of the total is wide because only 18 months of results exist. A FAIL here is "not shown", not "shown to lose".
- The look is spent: these months are no longer unread. The same months had been read at 1h on four pairs by earlier studies.
- Block rule P3 held exactly at its threshold (4 of 6).
