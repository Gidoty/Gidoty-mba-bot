# Profit protection v2, Part D: maximum holding time and reassessment

Status: built, OFF. Base branch: `feature/profit-staged-lock` (Part C), on B, on A, on `feature/ema-exit-rules` (PR #84).

## Settings (all 0 = OFF)
- `EMA_MAX_HOLD_CANDLES`: candles of the EMA strategy's timeframe (1h by default).
- `DONCHIAN_MAX_HOLD_CANDLES`: candles of the Donchian timeframe.
- `PROFIT_TIME_TIGHTEN_ATR` (default 1, clamped 0.5 to 10): used only at the limit.
- RSI keeps its own `RSI_MR_TIME_STOP_CANDLES` and is not changed.
Unset, empty, malformed, zero or negative is OFF. A typo can never switch a limit on. On is clamped to 1 to 10,000.

## What happens at the limit (live and paper, any account)
The candles held are `(now - opened) / timeframe`. At or past the limit, every round:
- Flat or in loss NET of costs (the price is not beyond the entry by more than the cost buffer: both fees, assumed slippage): the trade exits through `lifecycle.execute_exit` with the close reason `time_stop`, a market exit. The exchange-side stop is cancelled by the normal exit path.
- In profit net of costs: the stop is tightened to the best price minus `PROFIT_TIME_TIGHTEN_ATR` x ATR, never below entry plus the buffer (so a stop-out is at or above zero net). The move goes through `profit_protect.decide` and `lifecycle.move_stop`: never loosened, the Part B call limits apply, and if the price is already through the level the trade exits instead.
- Paper: the same decision. In profit the stop is ratcheted (never loosened, set at the price if the price is already through the level), flat or in loss the paper trade is closed at the price with `time_stop`.

## Honest limits
- It is a time rule, not a profit rule. A trade that is flat after the limit but about to move is closed anyway. That cost is measured in Part H.
- The limit is judged each round (5 minutes plus the round time), so the exit comes up to one round late.
- The exit is at the market: the fill can be worse than the price the decision used. It is not a guarantee.
