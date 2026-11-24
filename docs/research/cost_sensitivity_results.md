# Cost sensitivity: results

Plan hash `915faca89a86f2ed` (registered in commit `e41a4c8` before the run). The SAME trades as the cost split, re-priced; average net R per filled trade. Windows: A = 57 months to 2024-12, C = #102's nine test windows (2022-04 to 2026-09); window B and every interval are in `docs/research/cost_sensitivity_table.md`. **The fee schedule and the BNB discount are UNVERIFIED** (the published schedule as I know it, not read from the exchange). Not a prediction.

| scenario | EMA A | EMA C | Donchian A | Donchian C | RSI A | RSI C |
|---|---|---|---|---|---|---|
| baseline (taker both sides, regular tier) | +0.058 | -0.123 | -0.173 | -0.174 | -0.266 | -0.285 |
| fees paid in BNB (-10%) | +0.064 | -0.116 | -0.165 | -0.165 | -0.258 | -0.276 |
| VIP 2 taker (needs large volume) | +0.076 | -0.102 | -0.149 | -0.146 | -0.243 | -0.257 |
| VIP 3 and BNB (needs large volume) | +0.083 | -0.094 | -0.140 | -0.134 | -0.234 | -0.246 |
| maker entry, no missed fills | +0.109 | -0.064 | -0.106 | -0.093 | -0.201 | -0.206 |
| maker entry, 20% missed, random subset | +0.109 | -0.064 | -0.106 | -0.093 | -0.201 | -0.206 |
| maker entry, 20% missed, the best 20% of trades missed | -0.877 | -0.921 | -0.610 | -0.619 | -0.400 | -0.400 |
| maker entry, 40% missed, the best 40% of trades missed | -1.043 | -1.050 | -0.847 | -0.863 | -0.743 | -0.740 |
| maker entry and BNB | +0.113 | -0.059 | -0.101 | -0.087 | -0.196 | -0.199 |
| maker entry, VIP 3 and BNB (best case) | +0.127 | -0.043 | -0.082 | -0.064 | -0.178 | -0.177 |
| no trading cost at all (gross plus funding) | +0.184 | +0.023 | -0.008 | +0.025 | -0.106 | -0.088 |

## What the numbers say
- **No fee or order-type change turns Donchian or RSI positive.** Even with NO trading cost at all (gross plus funding) Donchian is -0.008 R (A) and +0.025 R (C) per trade and RSI -0.106 and -0.088: the signal itself earns about nothing (Donchian) or loses (RSI). The best realistic case here (maker entries, VIP 3 and BNB) leaves Donchian at -0.082 and -0.064 and RSI at -0.178 and -0.177.
- **EMA is the only one with room.** In A it is already +0.058 at the regular tier and +0.127 in the best case; over #102's windows (C) it is -0.123 at the regular tier, -0.094 with VIP 3 and BNB, -0.064 with maker entries, and -0.043 in the best case; only with zero cost does C turn (+0.023). So fees and order type alone do not make EMA positive in C either.
- **Paying fees in BNB** is worth about +0.006 to +0.009 R per trade (a 10% fee cut); **a VIP tier** +0.01 to +0.04 R but needs 30-day volume far above a pilot account's; **maker entries** are worth +0.05 to +0.08 R per trade IF every order fills, because they save the entry slippage and half or more of the entry fee.
- **Missed fills decide whether maker entries help.** If a missed order is a random subset, the average of the filled trades is unchanged and you simply get fewer trades (20% missed: 20% fewer). If the missed orders are the trades that ran away, the result collapses (-0.40 to -0.92 R per filled trade with 20% missed, -0.74 to -1.05 with 40%): this reading drops the best 40% of trades by gross R, which is an extreme bound and not a forecast. Real adverse selection is somewhere between the two and is not measurable from candles.

## Which costs the owner can change and which not
- **He can change:** the fee tier is partly his (BNB payment is an account setting; a VIP tier only with volume he does not have); the order type (maker versus taker entries is a code change with the missed-fill risk above).
- **He cannot change:** slippage on stops and market exits (the 11 bps round trip is an assumption of the studies; a stop must execute when price falls, so it fills at the market; a stop-limit lowers slippage but can fail to fill in a gap, which is a safety trade-off, not tested here), and funding (set by the market; here 0.002 to 0.021 R per trade, the largest for EMA's long holds).

## Limits
Fee schedule and BNB discount UNVERIFIED; the maker-entry fill at the next open's price is a convenience (a real limit order fills at its price only if price trades there, and a taker entry is what the live bot places today); intervals are iid over trades (too narrow); archive data UNVERIFIED against the live API.
