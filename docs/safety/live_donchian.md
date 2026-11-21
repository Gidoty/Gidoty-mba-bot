# Live Donchian breakout with a trailing stop (B5b)

Status: on a branch (stacked on the live RSI PR), not merged, not deployed. Ships OFF (`LIVE_DONCHIAN`, default off). CLAUDE-RUN against a FAKE exchange; no profit is claimed and the strategy is unproven.

## Switch OFF
Identical to today: `donchian_breakout_v1` is paper only; a live account never reaches the Donchian code (a test poisons it).

## Switch ON, what changes
An account opted in to `donchian_breakout_v1` (owner-set `extra_strategies`), LIVE or DEMO, with `DONCHIAN_ENABLED=true`, `NEW_STRATEGY_GUARDS_MODE=enforce` and `FEE_GUARD_MARGIN` above 0:
* **Entry:** a close beyond the 20-candle channel with volume 1.5 x average and ATR not extreme, on closed candles, through the same `_maybe_enter` and `lifecycle.open_live_trade` as EMA (kill switch, daily dollar limit, 50% total stop, guards, fee guard, pre-entry protection check, leverage cap, risk = smaller of the account's and `DONCHIAN_RISK_PCT`, protective stop placed and verified). Spot long only. Stored `strategy_id=donchian_breakout_v1` and `initial_stop_price` (the stop it opened with).
* **Priority for the one live slot:** Donchian first, then EMA, then RSI. If Donchian enters, EMA and RSI wait.
* **Trailing stop (`lifecycle.move_stop`):** from the closed candles after the entry the trail is the extreme since entry minus 2 x ATR, ratcheted. The resting protective stop is moved to it only if it is **better** than the stop resting now (up for a long, down for a short) and still on the losing side of the price. **The new stop is placed first and read back resting; only then is it recorded on the trade; only then is the old stop cancelled.** If placing or verifying the new stop fails (any error), the old stop is untouched, the trade stays open and protected, any half-placed new order is cancelled, and the customer gets one alert (not repeated for 6 hours): "Could not move the trailing stop... Your existing stop-loss at X is still in place." If only the old cancel fails, the move stands (two stops rest, which is safe) and the leftover is cleared when the trade closes.
* **Exit:** a CLOSE beyond the opposite 10-candle channel exits through `lifecycle.execute_exit` (reason `channel_exit`). A touch of the initial or trailing stop is the exchange-side stop's job. An EMA crossover never closes a Donchian trade. An open Donchian trade is managed even if the switch is turned off.

## Binance mechanics (UNVERIFIED)
* On Futures the protective stop is a `closePosition` stop-market order. Binance may refuse a second `closePosition` stop in the same direction while one rests. The venue already falls back to a `reduceOnly` stop with a quantity, so the new stop can still be placed first; if both are refused the move fails safely (old stop kept, alert). Which of these Binance actually does is not verified. The demo runner (PR 2) is the place to find out before live.
* On Spot the first stop locks the held quantity, so a second stop cannot be placed first. The move is then refused and the initial stop stays; trailing effectively does not run on Spot. This is the safe failure, and it means the trailing stop is a Futures feature in practice until a Spot method (such as an OCO amend) is designed.
* A crash between placing the new stop and recording it leaves an extra resting stop; it is cleared when the trade closes. Fills of that extra stop would not be counted in the exit price (rare, noted).

## Where the code is
The trailing level is `donchian_trail.py`, not `donchian.py`: that file's hash is part of the pre-registered backtest plan hash, so editing it would void the registration.

## Migration `b7f3d5a9c2e4` (parent `a6e2c4f8b1d3`)
`trades.initial_stop_price`, nullable float. Additive, idempotent, reversible. Not run. Written for every new live trade; read only by the trailing code.

## Owner steps and rollback
`DONCHIAN_ENABLED=true`, `NEW_STRATEGY_GUARDS_MODE=enforce`, `FEE_GUARD_MARGIN` above 0, the account's `extra_strategies` set by you, then `LIVE_DONCHIAN=true` with restart gate and restart. Rollback: remove the line and restart (an open trade keeps being managed); `alembic downgrade a6e2c4f8b1d3` drops the column.

## NOT verified
Profit; Binance behaviour on second stops (above); Spot trailing; the screens (none changed).

## Auto Select
An Auto Select account opted in to this strategy now has it evaluated every round on every candidate pair, whether or not EMA found a signal: see `docs/safety/auto_select_strategies.md`.
