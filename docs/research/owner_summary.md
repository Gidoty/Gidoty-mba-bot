# What the evidence says (facts and limits only)

Numbers are R per trade (R = the amount at risk: entry to initial stop), after 5 bps taker fee per side, 11 bps round-trip slippage and historical funding; BTC, ETH, BNB, XRP; 1h unless stated; 2020 to 2026. Costs are assumptions of the studies, not measurements of your account. All figures are past data, not forecasts.

**The strategies as they run (1h).** Donchian -0.17 R per trade (5,296 trades), negative in all 9 six-month windows. RSI -0.29 R (624 trades), negative in all 9. EMA -0.12 R (2,659 trades), positive in 4 of 9, not distinguishable from zero (+0.06 in 2020 to 2024, -0.30 in 2025).

**Costs or signal.** Costs take 0.14 to 0.20 R per trade (fees 0.06 to 0.09, slippage 0.07 to 0.10, funding up to 0.02). Donchian's signal earns about 0.00 R before costs: its loss is the cost. RSI's signal loses 0.09 to 0.11 R before costs: about a third of its loss is the signal. EMA's signal earned +0.2 R before costs in 2020 to 2024 and +0.03 over 2022 to 2026.

**Possible changes.**
- *Fees and order type:* fees in BNB add about +0.006 to +0.009 R; maker entries up to +0.05 to +0.08 R if every order fills (missed fills can erase it); a VIP tier needs volume far above a pilot's (fee schedule UNVERIFIED). Best case, Donchian is still -0.06 to -0.08 and RSI -0.18; at zero cost Donchian is about 0.00 and RSI -0.09 to -0.11. Slippage on stops and funding are not his to change.
- *Exit rules (+1R lock, take profit, trail):* no consistent net gain for EMA, a loss for Donchian, nothing for RSI (its target is hit first). The lock cuts give-backs of a +1R profit (EMA 50% to 64% down to 2% to 6%).
- *Entry filters, guards, pair choice:* none of 31 candidates passed; the best was within noise of doing nothing.
- *4h and 1d candles:* cost per trade falls to 0.05 to 0.12 R. EMA there is +0.24 to +0.72 R per trade (2022 to 2025) but not distinguishable from zero (a few windows carry it); Donchian +0.01 to +0.11; RSI 4h 0.00. Nothing passed the pre-registered rule; the later windows were not opened.

**The 100-trade pilot.** One trade's R varies widely (standard deviation 0.8 RSI, 1.4 Donchian, 2.8 EMA 1h, 4.8 EMA 4h). A 100-trade average is accurate to about plus or minus 0.16 R (RSI), 0.27 (Donchian), 0.55 (EMA 1h), 0.94 (EMA 4h). Resampling the real trades 20,000 times, a 100-trade pilot ends with a positive total in about 0% of cases for RSI, 10% Donchian, 29% EMA 1h (80% EMA 4h on 2020 to 2025 trades, an edge that is not established), and ends below -10 R in 99%, 72%, 59% of cases for RSI, Donchian, EMA 1h. If the history holds, 100 trades can show a clearly negative result for RSI and Donchian and cannot separate EMA from zero. Time to 100 trades on one pair: Donchian 1h about 4 months, EMA 1h 8, RSI 1h 34, EMA 4h 33, EMA 1d about 19 years.

**Limits.** Costs, fee schedule, maker fills and the data (public Binance files, not the live API) are assumptions or UNVERIFIED. 31 entry candidates and 9 timeframe cells were tested; a pass by chance was possible and none occurred. Another market regime may differ. The 4h and 1d figures rest on far fewer trades.
