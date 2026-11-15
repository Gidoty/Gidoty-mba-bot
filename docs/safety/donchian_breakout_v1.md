# donchian_breakout_v1: Donchian channel breakout (PAPER ONLY, OFF by default)

Built on `feature/rsi-mr-v1` (PR #48). Nothing here is deployed; no order of any kind is placed (paper trades are database rows). Every number below is
the specification, not a tuned or measured value. Backtest results: NOT MEASURED until the owner runs `python -m mba_bot.donchian_backtest` on real candles
(docs/research/donchian_preregistration.md).

## Rules (src/mba_bot/donchian.py, pure)

* Timeframe `DONCHIAN_TIMEFRAME` (default 1h). Closed candles only: the forming candle is dropped before anything is computed.
* Long: the last closed candle CLOSES above the highest high of the 20 candles before it (`DONCHIAN_ENTRY_PERIOD`). Short (Futures only): it closes below their lowest low. Spot is long only.
  A wick beyond the channel with a close back inside does not count. The close must be strictly beyond the line.
* Volume: the breakout candle's volume must be strictly above `DONCHIAN_VOLUME_MULT` (1.5) x the average volume of those same 20 candles (the breakout candle is not in its own average).
* Skip when ATR% (ATR(14) / close) is strictly above `DONCHIAN_ATR_EXTREME_MULT` (3.0) x the average ATR% of the 20 candles before the breakout candle.
* Stop: the channel midline or `DONCHIAN_STOP_ATR` (1.5) x ATR from entry, whichever is closer to entry. The entry price is the last closed candle's close.
* Exit, first event wins, evaluated candle by candle on the closed candles after entry:
  1. the stop, against the candle's low (long) or high (short), using the level set by the candles before it;
  2. a trailing stop of `DONCHIAN_TRAIL_ATR` (2.0) x ATR measured from the highest high (long) or lowest low (short) since entry. It uses the ATR of each candle, only ever moves towards the
     trade, and takes over from the initial stop once it is tighter. Reason `trailing_stop`;
  3. at the close, a close beyond the opposite 10-candle channel: below the lowest low of the previous 10 candles for a long, above the highest high for a short. Reason `channel_exit`.
  The initial stop's reason is `stop_loss`. There is no profit target and no time stop. When one candle touches the stop and would also move the trail, the stop wins.
* Cooldown: no new Donchian entry on a pair for `DONCHIAN_STOP_COOLDOWN_HOURS` (4) after a trade of this strategy on that pair was closed with reason `stop_loss`. A trailing stop or a channel exit does not start it.
* Fee guard (PR #45): the market fees (Spot 0.10, Futures 0.05 percent per side) plus 0.05 percent slippage per side. The strategy has no profit target, so the expected move is the same
  `FEE_GUARD_EXPECTED_MOVE_ATR` proxy the EMA strategy uses (default 1.0 ATR).
* Risk per trade: the smaller of the account's own risk % and `DONCHIAN_RISK_PCT` (0.5; the EMA default is 1.0).

Readings of the specification, for your confirmation: "close back inside the opposite 10-candle channel" is implemented as a close beyond the opposite 10-candle extreme (the classic
turtle exit). The 20-candle volume average and the "rolling average" of ATR% both use the 20 candles BEFORE the breakout candle.

## Switches

| Setting | Default | Meaning |
|---|---|---|
| `DONCHIAN_ENABLED` | false | Off: the strategy-aware path is never reached for it, nothing changes. A test makes every Donchian function raise with the switch off. |
| `DONCHIAN_PAPER_ONLY` | true | Informational. donchian_breakout_v1 is paper only IN CODE: a live account never runs it, whatever this or any setting says (test, both values). |
| `NEW_STRATEGY_GUARDS_MODE` | off | donchian_breakout_v1 does NOT trade unless this is `enforce` (fail closed: `donchian_guards_off` when off, `donchian_guards_not_enforced` in observe, which blocks nothing; logged once a day). `STRATEGY_GUARDS_MODE` is the EMA strategy's own mode and does not affect this strategy (audit H2/M1). |
| `trading_accounts.extra_strategies` | NULL | Per-account opt-in, set by the owner in the database (for example `donchian_breakout_v1`; comma separated for both new strategies). NULL means EMA only: the account keeps Auto Select and the EMA path. No API or screen writes it. A live account ignores it. |
| `FEE_GUARD_MARGIN` | 0 | It does NOT trade while this is 0 (`donchian_fee_guard_off`). |

## How the guards of PR #45 apply (not copied; `strategy_guards.evaluate` takes the strategy id)

Kill switch: live entries only, so not relevant to a paper-only strategy. The fee guard and cooldown as above. Daily loss limit, expectancy pause and the streak pause (`STRATEGY_PAUSE_AFTER_LOSSES`,
off by default) count ONLY this strategy's own closed trades. The EMA gap rule and the EMA cooldown setting are EMA-only and do not apply. Portfolio caps (`MAX_OPEN_POSITIONS`,
`MAX_TOTAL_RISK_PCT`) apply to every strategy. Observe mode logs "would block" and blocks nothing; enforce blocks the new entry.

## Slot rule (Donchian first, as reserved)

The strategy-aware cycle asks the strategies in this order: **Donchian, EMA, RSI** (only those switched on; EMA is always asked). For each strategy, for each free pair in pair order, the first signal
that the guards and caps do not refuse opens the trade and the cycle stops: one new entry per cycle in total.

* Two or three strategies signal on the same pair: Donchian takes it; the others are logged `strategy_slot_lost_to` (journal; the decision log, see "known limits").
* The Donchian signal is refused (cooldown, a guard): EMA gets its turn in the same cycle, then RSI.
* Strategy order beats pair order. A held trade blocks every other strategy on that pair. A trade is always managed by the rules of the strategy that opened it, also after the switch is turned off.
* Starvation: a Donchian trade has no time stop and can stay open as long as the trend lasts; with one slot it blocks EMA and RSI meanwhile. Breakouts are rarer than RSI crosses, so priority first
  rarely starves the others; the grade and `MAX_OPEN_POSITIONS` of PR #47 set how many trades may be open.

A paper account that the owner opted in (`extra_strategies`) while `DONCHIAN_ENABLED` is on uses this path even with one pair and one slot (and no longer scans Auto Select). A paper account that is not opted in, and every live account, never does.

## Known limits

* Breakout strategies are usually low win rate with a few large winners, and whipsaw in ranges. The volume filter and the cooldown reduce false breakouts; they do not remove them. A close beyond the
  channel with high volume can still reverse the next candle.
* The decision log de-duplicates rows on (account, pair, stage, outcome, reason, candle) WITHOUT the strategy id, so when two lower-priority strategies lose the slot on the same pair in the same
  cycle only the first one's `strategy_slot_lost_to` row is kept. The journal line is complete. Not changed here (it is in code from before this PR); a one-line fix is proposed in the PR description.
* No `TradeTelemetry` row is written for Donchian trades (that table is shaped for the EMA signal). Decision-log rows carry `strategy_id`.
* The live cycle sees at most `OHLCV_LIMIT` (200) candles; ATR smoothing can differ slightly from the backtest's full history. Paper exits fill at the stop or trail level with no slippage (the backtest applies slippage).
* The trailing stop is recomputed from the candles after entry on every cycle, so it does not depend on when cycles ran. It needs ATR history before the entry; with 200 candles that is available.

## Owner steps (none done by Claude)

1. Deploy with `DONCHIAN_ENABLED` unset (nothing changes). No migration in this PR beyond PR #47's.
2. For ONE paper test account: set its `extra_strategies` to `donchian_breakout_v1` in the database, then `NEW_STRATEGY_GUARDS_MODE=enforce`, `FEE_GUARD_MARGIN` above 0 (for example 1.5), `DONCHIAN_ENABLED=true`; `python -m mba_bot.restart_gate` before any restart.
3. Watch the journal for `donchian_breakout_v1` and `strategy_slot_lost_to` lines. Move to `enforce` only after reading what observe would have blocked.
4. Rollback: `DONCHIAN_ENABLED=false`. An open Donchian paper trade keeps being managed by its own rules until it exits.
