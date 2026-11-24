# Profit protection v2, Part E: reversal exits for EMA trades

Status: built, OFF. Base branch: `feature/give-back-alarm` (Part G), then F, D, C, B, A, and `feature/ema-exit-rules` (PR #84).

## The two rules (EMA trades only, closed candles only)
R is the distance from the entry to the INITIAL stop. The best price is `trades.peak_price` (Part A).
1. `EMA_REVERSAL_FAST_CROSS=true`: after the trade has been in profit (best price at least `EMA_REVERSAL_MIN_PROFIT_R` x R beyond the entry, default 0.5), a candle that CLOSES back across the fast EMA (below it for a long, above it for a short) ends the trade. Close reason `reversal_fast_ema`.
2. `EMA_REVERSAL_GAP_FRACTION` (0 = off, 0.1 to 0.95): the fast/slow gap in the trade's favour is measured on every closed candle since the entry. Once its peak is positive, a candle whose gap is at or below (1 - fraction) of that peak ends the trade. Close reason `reversal_gap`. Candles from before the entry never count toward the peak.
Both are pure functions in `ema_reversal.py` (no I/O). A typo in a switch means OFF.

## Where they run
- Live: `lifecycle.execute_exit` (the only live exit path), after the strategy's own rules, the staged lock and the time stop, for a healthy protected EMA trade. Donchian and RSI trades are never judged by these rules.
- Paper: the trade is closed at the price with the same reason (a market exit), in the single-trade and multi paper paths.
- The paper REPLAY (`paper_replay.py`) is NOT extended for these rules: it replays the exit rules over a trade list with the closed candles at hand but has no EMA series per trade. Part H's sealed test measures these rules on the candles instead.

## Limits
- Closed candles, so the exit comes after the candle that shows the reversal, at the market. On 1h candles a reversal can give back a lot before the candle closes; the exchange-side stop and the staged lock are what protect inside a candle.
- Nothing here has been shown to improve results. Part H reports whether each rule lowers net profit.
