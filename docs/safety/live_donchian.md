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

## Binance mechanics (what is known, what is not)
* **What the bot sends.** On Futures the protective stop is a stop-market order with `closePosition`. With the pinned ccxt (4.5.88) a conditional order on a USDS-M symbol is sent to Binance's **Algo Order** endpoint (`POST /fapi/v1/algoOrder`, `algoType=CONDITIONAL`; ccxt `create_order` -> `isAlgoOrder`), looked up and cancelled by `clientAlgoId` on the same family of endpoints, and its `FINISHED` and `TRIGGERED` statuses map to `closed`. This is read from the ccxt source in this repository's environment (`ccxt/binance.py`, `create_order`, `cancel_order`, `parse_order_status`), not from Binance's pages.
* **The live defect (27 Nov 2026).** The move of the stop failed about 74 times in a row with a plain `ExchangeError` while a `closePosition` stop rested. The pinned ccxt has **no entry for Binance code -4130** in its USDS-M error table (`exceptions['linear']['exact']`), so that code surfaces as a plain `ExchangeError` whose text is the id plus Binance's JSON answer. The old fallback to a `reduceOnly` stop caught only `InvalidOrder` and `BadRequest`, so it never ran. The text of code -4130, as remembered by the ccxt source and by the Binance error list, is "An open stop or take profit order with GTE and closePosition in the direction is existing". **That this is the code the live refusal carried is UNPROVEN**: the journal logged only the exception class. The journal now logs the code and message (truncated to 200 characters, nothing else), so the next occurrence proves or disproves it.
* **The fix.** The new stop is still placed first and read back resting; only then is the old one cancelled. If `closePosition` is refused, the same call places a `reduceOnly` stop sized from the exchange-confirmed position. The fallback is taken for code -4130, for `InvalidOrder` and `BadRequest` (as before) and for a plain `ExchangeError`; never for network or authentication errors. If both placements fail the old stop is untouched. A failed move is retried after 5 minutes, then 15, then at most every 30 minutes per trade (a single transient failure costs 5 minutes, not 30).
* **Not verified against Binance.** Binance's documentation pages (developers.binance.com) could not be read from the build environment (the site answers with a bot challenge), so the endpoint, error code and `closePosition` / `reduceOnly` rules above are from ccxt's source, not quoted from Binance. `ops/testnet/check_second_stop.py` is an owner-run check on Binance Futures Demo Trading that records the exact refusal; demo may differ from production, and the real confirmation is a live trade where the lock moves a stop.
* **Whether a `reduceOnly` stop is sized right after the position changes.** It is sized from the position when it is placed. The bot never adds to a position; if the customer changes the position by hand afterwards, a `reduceOnly` stop keeps its old size until the next move places a new one.
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
