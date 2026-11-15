# rsi_mr_v1: RSI mean reversion (PAPER ONLY, OFF by default)

Built on `feature/multi-trade-foundation` (PR #47). Nothing here is deployed; no order of any kind is placed (paper trades are database rows).
Every number below is the specification, not a tuned or measured value. Backtest results: NOT MEASURED until the owner runs
`python -m mba_bot.rsi_backtest` on real candles (docs/research/rsi_mr_preregistration.md).

## Rules (src/mba_bot/rsi_mr.py, pure)

* Timeframe `RSI_MR_TIMEFRAME` (default 1h). Closed candles only: the forming candle is dropped before anything is computed.
* Long: RSI(14) was below 30 on the previous closed candle and is above 30 on the last one. Short (Futures only): RSI was above 70 and is below 70. Spot is long only.
* Range filter: only when ADX(14) is below `RSI_MR_ADX_MAX` (25). Missing ADX or ATR means no signal.
* Stop: `RSI_MR_STOP_ATR` (1.5) x ATR(14) from entry. Risk per trade: the smaller of the account's own risk % and `RSI_MR_RISK_PCT` (0.5; the EMA default is 1.0).
* Exit, first event wins, evaluated candle by candle on the closed candles after entry: stop; profit of `RSI_MR_TARGET_ATR` (1.0) x ATR; RSI back to 50 (at the close);
  time stop after `RSI_MR_TIME_STOP_CANDLES` (24) candles. When one candle touches both stop and target the stop wins. The entry ATR is recovered from the stored stop
  (stop distance / 1.5), so no new column is needed.
* Fee guard: skipped when expected profit to target is below `FEE_GUARD_MARGIN` x round-trip cost. Cost = 2 x fee (Spot 0.10 percent, Futures 0.05 percent per side) + 2 x 0.05 percent slippage.
* Pause after `RSI_MR_PAUSE_AFTER_LOSSES` (2) consecutive net losses on the SAME pair: that pair is paused for this strategy (`STRATEGY_STREAK_PAUSE_HOURS`, default 24; the owner can release it).

## Switches

| Setting | Default | Meaning |
|---|---|---|
| `RSI_MR_ENABLED` | false | Off: the strategy-aware path is never reached for it, nothing changes. A test makes every RSI function raise with the switch off. |
| `RSI_MR_PAPER_ONLY` | true | Informational. rsi_mr_v1 is paper only IN CODE: a live account never runs it, whatever this or any setting says (test). |
| `STRATEGY_GUARDS_MODE` | off | rsi_mr_v1 does NOT trade while this is `off`: it needs `observe` or `enforce` (fail closed, logged once a day as `rsi_guards_off`). |
| `FEE_GUARD_MARGIN` | 0 | rsi_mr_v1 does NOT trade while this is 0 (`rsi_fee_guard_off`). |

## How the guards of PR #45 apply (not copied; `strategy_guards.evaluate` takes the strategy id)

Kill switch: live entries only, so not relevant to a paper-only strategy. Fee guard with the strategy's own fees and target. Daily loss limit, expectancy pause and
streak pause use ONLY this strategy's own closed trades (`closed_results(strategy_id=...)`); the EMA strategy's statistics no longer include RSI trades (an empty
strategy id on a row is EMA). The EMA gap rule and the EMA stop cooldown are EMA-only and are skipped. Portfolio caps (`MAX_OPEN_POSITIONS`, `MAX_TOTAL_RISK_PCT`) apply to
every strategy. Observe mode logs "would block" and blocks nothing; enforce blocks the new entry.

## How the signals reach the trading path, and the slot rule

Until now the strategy-aware (multi-trade) cycle looked at the EMA signal only. It now builds a list of strategies in priority order and asks each for a signal:

1. Donchian (reserved first place; NOT BUILT).
2. `ema_cross_atr_v1`: unchanged, on `TIMEFRAME`, including the forming candle exactly as before.
3. `rsi_mr_v1` (only if `RSI_MR_ENABLED`): on `RSI_MR_TIMEFRAME`, closed candles only.

For each strategy in that order, for each free pair in the account's pair order, the first signal that the kill switch, the guards and the caps do not refuse opens the trade, and the
cycle stops: **one new entry per cycle in total**. So:

* Two strategies signal on the same pair in the same cycle: the higher priority takes it. The other is logged as `strategy_slot_lost_to` (journal and decision log), not queued.
* The higher-priority signal is refused (a guard, a pause, the fee guard): the next strategy gets its turn in the same cycle.
* Strategy priority wins over pair order: an EMA signal on the second pair is tried before an RSI signal on the first.
* A held trade blocks every other strategy on that pair; the number of trades overall is the market grade and `MAX_OPEN_POSITIONS` of PR #47. A trade is always managed by the rules of the
  strategy that opened it (also after the switch is turned off), so an RSI trade is never exited by the EMA rules.
* Starvation: an RSI trade lasts at most 24 candles, an EMA trade has no time stop. With one slot an open EMA trade blocks RSI for as long as it lasts. RSI signals are also much more frequent
  than EMA crosses in a range, so a lower priority is what keeps RSI from filling the slot. The grade (PR #47) is computed from all paper trades of the account together.

A paper account with `RSI_MR_ENABLED` uses this path even with one pair and one slot. A live account never does.

## What this does NOT do / known limits

* No `TradeTelemetry` row is written for an RSI trade (that table is shaped for the EMA signal); decision-log rows carry `strategy_id`. UNVERIFIED that this is enough for research use.
* The live cycle sees at most `OHLCV_LIMIT` (200) candles, so Wilder smoothing can differ slightly from the backtest's full history.
* Paper exits are modelled from candles: stop and target fill at their price with no slippage in the live paper path (the backtest does apply slippage).
* Mean reversion has a fat left tail: a stop caps one trade, not a regime, and ADX lags. The 1.0 ATR target against a 1.5 ATR stop needs a win rate near 60 percent before costs; costs are 0.20
  to 0.30 percent round trip, which is large against a 1h ATR target on the big pairs. Expect the fee guard to refuse many signals.
* Cross-strategy correlation is not capped beyond `MAX_TOTAL_RISK_PCT`.

## Owner steps (none done by Claude)

1. Deploy with `RSI_MR_ENABLED` unset (nothing changes). No migration in this PR beyond PR #47's.
2. For a paper test account: `STRATEGY_GUARDS_MODE=observe`, `FEE_GUARD_MARGIN` above 0 (for example 1.5), `RSI_MR_ENABLED=true`, restart in a quiet moment (`python -m mba_bot.restart_gate` first).
3. Watch the journal for `rsi_mr_v1` lines and `strategy_slot_lost_to`. Move to `enforce` only after reading what observe would have blocked.
4. Rollback: `RSI_MR_ENABLED=false`. An open RSI paper trade keeps being managed by its own rules until it exits.
