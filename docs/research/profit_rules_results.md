# Profit protection v2: sealed test results

Plan hash `9348815fefe80d40` (registered in commit 5464035 before any run). Data hash `59379bb81c8e0675`. Tune window 2025-11-01 to 2026-07-01; confirmation window 2026-07-04 to 2026-09-15, read ONCE (the look is recorded in the ledger beside the data). EMA strategy only, BTC, ETH, BNB, XRP pooled, 1h and 15m. Costs: 5 bps taker per side, 11 bps round-trip slippage on every leg, historical funding, 2 bps extra on each order beyond the entry and one exit. **This is a measurement on two windows, not evidence that a rule will make money.**

## The answer
1. **No rule set beats the current rules on net profit in both windows, on either timeframe.** Every option that was better than RS0 in one window was worse in the other. The pre-registered "best combination" was RS0 itself on 1h (nothing qualified on tune) and A3 on 15m (it was +23.5 R on tune and **-215.2 R against RS0 on confirm**).
2. **The give-back rate does fall, as designed, and consistently.** Any stage 1 lock cuts it from 50% (tune) and 64% (confirm) on 1h to 2% to 6%, and from 56% and 64% on 15m to 9% to 14%. This happens almost by construction: once a trade has reached +1 R its stop sits at entry plus costs, so it can no longer close below that, except through a gap.
3. **The price of that is winners cut short and, on most rule sets, net profit.** PR #84's rules (take profit, break-even, trail), the staged lock (B1), stage 1 beyond noise (P1), both trails (T1, M1), partial profit (I1) and the reversal gap exit (E2) lowered net R in both windows on 1h (and every rule set but A2 and A3 did on 15m in both windows).
4. **The same simple rule changed sign between windows.** Stage 1 at +1 R (A1) on 1h: -5.4 R against RS0 on tune, +33.2 R on confirm. With 139 to 371 trades per window and a baseline that is itself near zero (RS0: -0.033 R per trade on tune, +0.112 on confirm) that is what noise looks like. I did not compute confidence intervals. I would not switch anything on from this.
5. **The baseline is the larger problem.** On 15m RS0 loses 380 R on tune and wins 27.6 R on confirm; no profit rule fixes a strategy whose edge after costs is about zero.

## Net total R against RS0, tune and confirm side by side (pairs pooled, after costs and funding)
| rule set | 1h tune | 1h confirm | 15m tune | 15m confirm |
|---|---|---|---|---|
| RS0 (net total R) | -12.4 | +15.6 | -380.0 | +27.6 |
| RS84 (PR #84) | -31.5 | -42.5 | -124.1 | -282.2 |
| A1 stage 1 at +1R | -5.4 | +33.2 | -2.7 | -240.3 |
| A2 stage 1 at +1.5R | -16.0 | +21.2 | +22.2 | -284.5 |
| A3 stage 1 at +2R | -9.0 | +14.4 | +23.5 | -215.2 |
| B1 staged lock | -36.2 | -45.2 | -96.4 | -299.6 |
| P1 stage 1 beyond noise | -26.2 | -40.9 | -105.0 | -303.7 |
| T1 tiered trail | -20.5 | -41.0 | -89.7 | -316.7 |
| M1 trail tightened by time | -19.8 | -41.8 | -99.2 | -289.9 |
| D1 time stop (72 candles) | -39.2 | -1.1 | -100.4 | -228.7 |
| E1 close back across fast EMA | -19.1 | +11.9 | -195.1 | -270.8 |
| E2 gap shrinks 50% | -48.9 | -0.8 | -198.8 | -251.9 |
| I1 partial profit | -37.9 | -44.6 | -123.2 | -300.8 |
| J1 stage 1 + one re-entry | -22.3 | +44.9 | -95.2 | -307.2 |
(Each cell is the change in net total R against RS0 in that window; the first row is the absolute figure.)

## Give-back rate, winners cut short (tune / confirm)
| rule set | 1h give-back | 1h cut short | 15m give-back | 15m cut short |
|---|---|---|---|---|
| RS0 | 50% / 64% | 0 / 0 | 56% / 64% | 0 / 0 |
| A1 | 2% / 6% | 37 / 10 | 9% / 14% | 135 / 39 |
| A2 | 21% / 24% | 26 / 9 | 27% / 30% | 83 / 35 |
| A3 | 32% / 40% | 14 / 7 | 38% / 41% | 55 / 25 |
| B1, P1 | 2% / 6% | 54, 49 / 8, 8 | 9% / 14% | 204, 215 / 52, 57 |
| T1, M1 | 25%, 21% / 35% | 40, 43 / 6, 6 | 37%, 33% / 47%, 42% | 150, 159 / 33, 44 |
| I1 | 21% / 23% | 41 / 7 | 27% / 28% | 178 / 47 |
| E1, E2 | 24%, 37% / 28%, 45% | 43, 38 / 7, 6 | 43%, 50% / 54%, 58% | 120, 118 / 29, 34 |
| RS84 | 33% / 38% | 38 / 6 | 38% / 42% | 159 / 38 |

## Which options reduce the cost of the protection and which do not
The cost is the winners cut short and the net R given up. Measured against the plain stage 1 lock (A1):
- **A wider activation (A2, A3) cuts fewer winners** (1h tune 26 and 14 against 37) **but lets the give-back rate rise** to 21% to 40%. On 1h it did not earn more than A1 in either window (-10.6 / -11.9 R and -3.7 / -18.7 R against A1). On 15m A3 was ahead of A1 in both windows (+26.2 and +25.1 R), though still far behind RS0 on confirm. It is the only option that reduced the cost in both windows on one timeframe, and it does so by protecting less.
- **Stage 1 placed beyond noise (P1), the staged lock (B1), the tiered trail (T1) and the time-tightened trail (M1) do NOT reduce the cost.** They cut more winners than A1 on 1h tune (49, 54, 40, 43 against 37) and were behind A1 in every window and timeframe.
- **Partial profit (I1) does NOT.** It was behind A1 by 33 R to 121 R in every window and timeframe. Its extra order cost is small (1.0 R and 0.5 R on 1h, 8.4 R and 3.8 R on 15m); the loss is in what it gives up on the runner.
- **Re-entry (J1) recovered the move less than half the time and did not pay consistently.** 1h: 32 of 74 re-entries (43%) on tune and 16 of 36 (44%) on confirm. Against A1 it was -16.9 R on tune and +11.7 R on confirm on 1h, and -92.5 R and -66.9 R on 15m, where each re-entry is another full round trip of costs.
- **The reversal exits (E1, E2) and the time stop (D1)** gave an opposite sign between windows on 1h (E1: -19.1 then +11.9 R) and were worse on 15m in both.

## Limits (read before using any of this)
- Two windows, one strategy (EMA), four pairs. Donchian and RSI were not simulated, so the staged lock is unmeasured for them.
- The simulation acts at candle closes. The live bot looks every 5 minutes, so a rule that runs each round can act earlier inside a candle (it could cut the give-back further and also cut more winners). Not measured.
- Part B's minimum improvement and interval per stop move, stop-order fills other than the assumed slippage, and the account's real fee tier (5 bps assumed) are not modelled. The extra-order cost (2 bps) is an assumption.
- The give-back rate is judged against entry plus the ASSUMED costs, so it is an estimate.
- The tune window had been looked at for the baseline in PR #85. The confirmation window had never been read.
- Archive candles and funding come from `data.binance.vision`; UNVERIFIED that they match the live API exactly.
