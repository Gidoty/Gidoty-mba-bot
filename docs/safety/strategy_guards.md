# Strategy guards: limiting the downside of each strategy

Status: BUILT, tested offline, NOT deployed, every guard OFF by default. The effect of each guard on results is NOT MEASURED: no market
data was available to the author, and nothing here has been run on real data. This document lists what each strategy is known to be
bad at, what the code does about it, and what remains. It does not claim that any weakness is removed.

## What exists today
Only one strategy runs: `ema_cross_atr_v1` (EMA 20/50 crossover with an ATR flat-market filter and an ATR stop). `rsi_mr_v1` (RSI mean
reversion) and the Donchian breakout do not exist in the code, so their rows below describe the mitigations to build WITH those
strategies; none of that is coded yet. The shared controls are keyed by strategy id so they apply to a new strategy without change.
Trades carry no strategy id yet (there is one strategy), so today every trade of an account is treated as `ema_cross_atr_v1`. A second
strategy on the same account needs its trades tagged first (a separate, additive change).

## Switches (all default OFF; nothing changes for a customer until the owner enables them)
`STRATEGY_GUARDS_MODE`: `off` (default; the guards do not run), `observe` (every guard computes and LOGS what it would have refused,
nothing is blocked, nothing is written, nobody is alerted), `enforce` (the first guard that fires refuses the NEW entry). Unset, empty or
unrecognised means off. A threshold of 0 switches that single guard off. Enable one guard at a time, in observe first.
| Variable | Default | Guard |
|---|---|---|
| EMA_MIN_GAP_ATR | 0 (off) | EMA gap: fast and slow EMA at least this many ATRs apart when the entry is taken |
| EMA_STOP_COOLDOWN_HOURS | 0 (off) | no new entry on a pair for this long after a stop loss on it |
| FEE_GUARD_MARGIN | 0 (off) | expected move must be at least margin x (round-trip fees + slippage); with FEE_GUARD_FEE_BPS 5, FEE_GUARD_SLIPPAGE_BPS 11, FEE_GUARD_EXPECTED_MOVE_ATR 1 |
| MAX_OPEN_POSITIONS, MAX_TOTAL_RISK_PCT | 1, 2.0 | cap on open positions and on the total risk of open positions plus the new one (0 = no cap) |
| STRATEGY_DAILY_LOSS_PCT | 0 (off) | no new entries for the rest of the UTC day once the day's modelled net loss reaches this percent of capital |
| STRATEGY_PAUSE_AFTER_LOSSES, STRATEGY_STREAK_PAUSE_HOURS | 0 (off), 24 | pause the strategy for the account after N consecutive net losses, for these hours (0 = until the owner releases) |
| STRATEGY_EXPECTANCY_WINDOW | 20 | pause until the owner releases it if modelled net expectancy over the last N trades is negative (0 = off) |
| OWNER_ALERT_CHAT_ID | falls back to BILLING_ALERT_CHAT_ID | Telegram chat for owner alerts; without a bot token and a chat the alerts only go to the log |
Rollback: set `STRATEGY_GUARDS_MODE` to off (or unset it) and restart. Pauses already written stay in the table and are ignored while the mode is off.

## How it works
* New code: `src/mba_bot/strategy_guards.py` (pure decisions plus the few reads they need), one call in `trader._maybe_enter`, a `gap_atr` field on the strategy signal, a table `strategy_pauses` (migration `c9f6a3b1d5e7`, additive and idempotent, written only in enforce mode) and an owner command `python -m mba_bot.strategy_pause status|release`.
* Order inside `_maybe_enter`: the operator kill switch first (it stays authoritative; a guard can only add a refusal and never lifts one), then the pair-availability check, then the guards, then sizing. Managing an open position never goes through the guards: a paused strategy never stops an exit, a stop or the reconciler.
* The guards never place, change or cancel an order and import no order, venue or exchange code (a test pins it).
* Fail closed: in enforce mode a guard whose input cannot be read (no ATR, unreadable gap, no capital, a database error) refuses the entry with a specific code. In observe mode nothing is blocked.
* Incident trades (emergency exits after a failed protective stop, dust) are infrastructure events: they never count towards a streak, an expectancy or a daily loss. Paper and live results are kept apart, and a pause belongs to one account and one mode.
* Results are MODELLED: net = recorded pnl (gross) minus `FEE_GUARD_FEE_BPS` per side on entry and exit notional, in R (money at risk = |entry - stop| x quantity). The trading path does not record exact fees or funding.
* Pauses: a loss-streak pause lasts STRATEGY_STREAK_PAUSE_HOURS and then ends by itself; an expectancy pause has no end and only `strategy_pause release` ends it. After any pause ends only trades closed AFTER that moment count towards the same guard, so the old trades do not immediately undo a release. The owner is alerted once when a pause is written (and once per day for a daily limit).
* Reason codes (decision log and journal): `ema_gap_below_minimum` (a signal filter, never sent to the customer), `stop_loss_cooldown`, `fee_guard_expected_move_too_small`, `portfolio_cap_reached`, `strategy_daily_loss_limit`, `strategy_loss_streak_pause`, `strategy_expectancy_pause`, `strategy_paused`, `strategy_guard_unreadable`.
* Customer wording is fixed text with no numbers from the customer's own results.

## Weaknesses, mitigations, effect and what remains
Backtest effect: NOT MEASURED for every row. A runner that tunes on the first 4 months only and reports the last 2 months untouched, under a
new pre-registration, is delivered separately on the research branch; the owner runs it on his own data and the column is filled from his
output. Until then no row claims an effect.
| Strategy | Weakness | Mitigation (code) | Backtest effect | Remaining risk |
|---|---|---|---|---|
| ema_cross_atr_v1 | False crossovers in choppy markets | The existing ATR flat-market filter is kept. New: minimum EMA gap in ATRs (`EMA_MIN_GAP_ATR`), checked when the entry is taken. Because the cross stays true for the rest of the candle, a cross that widens later in the same candle can still enter | NOT MEASURED | A wider gap removes some false crosses and some good ones; whipsaws that open wide still pass; the filter says nothing about regime. Threshold untuned |
| ema_cross_atr_v1 | Late entries (the cross is a lagging signal) | None. The gap filter and the cooldown make entries later, not earlier | NOT MEASURED | Remains. It is the price of the filters: a late entry has a smaller reward for the same ATR stop |
| ema_cross_atr_v1 | Repeated stop-outs in the same chop | Cooldown after a stop loss on the same pair (`EMA_STOP_COOLDOWN_HOURS`) | NOT MEASURED | Skips real re-entries after a stop that reverses; only the same pair and the same account; a long chop longer than the cooldown still bleeds |
| rsi_mr_v1 (NOT BUILT) | Loses in strong trends (buying a fall that keeps falling) | To build with the strategy: ADX range filter, hard 1.5 x ATR stop, time stop, pause of the pair after 2 consecutive losses | NOT MEASURED (strategy does not exist) | Mean reversion has a fat left tail by nature; a stop caps one trade, not a regime; ADX lags |
| Donchian breakout (NOT BUILT) | False breakouts that reverse | To build with the strategy: volume confirmation, a CLOSE beyond the channel (not a wick), stop at the channel midline or 1.5 x ATR, skip when ATR is already extreme | NOT MEASURED (strategy does not exist) | Confirmation costs entry price; a close beyond the channel can still reverse; volume data quality on Futures is untested |
| All | Fee drag | Fee guard on every entry: expected move (`FEE_GUARD_EXPECTED_MOVE_ATR` ATRs) must exceed `FEE_GUARD_MARGIN` x (2 x taker fee + slippage) | NOT MEASURED | The strategy has no profit target, so "expected move" is a proxy; the account's real fee tier, BNB discount, funding and real slippage are not read; fees in results are modelled |
| All | Many strategies on the same market | One position per account is a database guarantee (unique index on non-terminal trades). `MAX_OPEN_POSITIONS` and `MAX_TOTAL_RISK_PCT` are the ceiling for when more than one can exist | not applicable (structural today) | The caps are per account. Customers all trade the same signal at the same time (correlated across accounts); nothing caps that. Risk per open position is a proxy (the configured risk percent) |
| All | Losing streaks | Per-strategy daily loss limit (`STRATEGY_DAILY_LOSS_PCT`) and a pause after N consecutive net losses (`STRATEGY_PAUSE_AFTER_LOSSES`), with an owner alert | NOT MEASURED | A streak pause ends by itself after the set hours and may resume into the same regime; N and the percent are untuned; the alert needs a Telegram bot token and chat |
| All | Overfitting | Procedure, not code: tune on the first 4 months only, report the last 2 months untouched, under a new pre-registration with a hash written before any result is seen | NOT MEASURED | Six months is short and probably one or two regimes; the sealed months are consumed the first time they are looked at; every extra variant raises the chance of a lucky pass |
| All | A strategy that stops working | Expectancy pause: after `STRATEGY_EXPECTANCY_WINDOW` (20) trades, a negative modelled net expectancy pauses the strategy for the account until the owner releases it; it never resumes by itself | NOT MEASURED | Twenty trades cannot tell a broken strategy from bad luck (false pauses will happen and a slow decay can pass); the scope is one account, so one failing strategy shows up as many separate pauses; fees are modelled |

## Tests
`tests/test_strategy_guards.py`: every boundary, off changes nothing, observe changes and writes nothing, enforce refuses a new entry with a specific code and places no order, the kill switch stays authoritative, pauses persist and only the owner ends an expectancy pause, incident trades and paper results are kept out, the owner command, the migration. All against a fake exchange: they prove control flow, not Binance behaviour, and not that any threshold helps.

## What is UNVERIFIED
The effect of every guard on real results; the thresholds (all provisional); the real fee tier and slippage; whether twenty trades is a useful window; behaviour on real Binance data; the effect on customers' experience of paused entries.

## Owner steps
Back up, run migration `c9f6a3b1d5e7` (new table only), deploy with the mode off. Set `STRATEGY_GUARDS_MODE=observe` with ONE threshold at a time (for example the expectancy window alone), restart, read the `[observe] strategy guard` journal lines or the decision log for a week, and only then decide on `enforce`. Release a pause with `python -m mba_bot.strategy_pause release` (see its docstring for the exact command).
