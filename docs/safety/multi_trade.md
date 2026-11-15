# Multi-trade foundation (paper accounts, up to 3 trades)

Status: built on branch `feature/multi-trade-foundation`, based on `feature/strategy-guards` (PR #45). CLAUDE-RUN: tests pass in the sandbox
against a FAKE exchange. Nothing is deployed, no migration has been run, no order has been placed.

## What it does

* A PAPER account can hold up to `MAX_OPEN_POSITIONS` trades at once (default 1, hard maximum 3), on different pairs, one trade per pair.
* A LIVE account is capped at 1 trade in code (`multi_trade.slot_cap`, `lifecycle.open_live_trade`), whatever the setting says.
* The number of allowed trades also depends on a market grade (1, 2 or 3). Any missing or unclear input is grade 1. A lower grade never
  closes a trade; it only blocks new entries.
* The owner sets the pair list for a test account directly in the database (`trading_accounts.pair_list`, e.g. `ETH/USDT,BNB/USDT`).
  No API, screen or onboarding step writes it. The account's own `pair` is always the first pair.
* At most ONE new entry per cycle, so three correlated entries never open in the same instant.
* `trades.strategy_id` is nullable. NULL or empty means `ema_cross_atr_v1`. No backfill.
* With the defaults (`MAX_OPEN_POSITIONS=1`, no `pair_list`) and for every live account, the multi-trade code is never reached. Tests prove it
  by making the multi-trade entry point raise.

## Grade (starting values, NOT tuned on any data)

| Grade | Trades | Conditions (all must hold) |
|---|---|---|
| 1 | 1 | Default. Also: any input missing, flat market, ATR% above 3x its rolling average, a strategy pause active, fewer than 10 closed paper trades, last 10 net negative after modelled fees |
| 2 | 2 | Flat filter passes, volatility not extreme, no pause, last 10 closed trades not net negative |
| 3 | 3 | Grade 2, plus the last 20 net positive, and today's loss under half of `STRATEGY_DAILY_LOSS_PCT` (that setting must be above 0, otherwise the most it gives is 2) |

Settings: `GRADE2_MIN_TRADES` (10), `GRADE3_MIN_TRADES` (20), `GRADE_EXTREME_ATR` (3.0), `GRADE_DAILY_LOSS_FRACTION` (0.5), `PAPER_MIN_NOTIONAL` (10).
A fresh paper account has no history, so it stays at grade 1 (one trade) until it has 10 closed trades. That is deliberate. To exercise
three slots on a test account before then, lower the two `GRADE*_MIN_TRADES` settings on that test deployment, or seed history.

## Checks before the 2nd and 3rd entry (paper)

Slot free for the grade; total open risk (money between entry and stop, from the stored trades) plus the new trade within
`MAX_TOTAL_RISK_PCT`; free capital (balance minus the notional of open trades, counted at 1x) above zero, with the quantity trimmed to
what is free; notional at least `PAPER_MIN_NOTIONAL`. A refusal is logged with a reason code (`trade_slots_full`, `total_open_risk_cap`,
`free_capital_short`) and the decision log when telemetry is on. The first trade of an account is never touched by these checks.
`PAPER_MIN_NOTIONAL` is a stand-in: paper has no exchange, so the real Binance minimum is not read. UNVERIFIED.

## Database

* `trades`: the unique index `uq_trades_one_nonterminal_per_account` is replaced by `uq_trades_one_nonterminal_per_pair` (account, pair) plus
  `uq_trades_one_nonterminal_no_pair` (account, only for rows with no pair, the old rule for legacy rows).
* New nullable columns: `trades.strategy_id`, `trading_accounts.pair_list`.
* Migration `a1d7c4e9b2f6`, parent `c9f6a3b1d5e7`. Additive, idempotent, reversible. The downgrade REFUSES if any account has more than one
  non-terminal trade (close or void the extras first). Claude has not run it.
* `init_db()` runs `create_all()`, which does not add columns to an existing table. The new code reads `trades.strategy_id` and
  `trading_accounts.pair_list`, so **run the migration before restarting the service on the new code**, or every trade query fails.

## Where a single trade was assumed (audit) and what was done

See the pull request description for the full list. In short: `db.get_open_trade` (now ordered, oldest first; `db.list_open_trades` added),
`trader.process_account`, `trader.reconcile_account`, `trader._maybe_enter`, `trader._maybe_exit`, `lifecycle.open_live_trade`,
`scheduler._recover_one` / `monitor_stop_limits_once`, `strategy_guards.evaluate`, `telemetry.log_decision`, the account status API and the
position API.

## What this does NOT do

* No frontend change. `/api/account/position` still describes the OLDEST open trade. A new `/api/account/positions` lists every open trade;
  no screen uses it yet.
* Auto Select is ignored for an account that uses a pair list.
* Guard statistics (loss streak, daily loss, expectancy) pool all of the account's trades. Only the stop cooldown is per pair.
* Three crypto trades at once are often one bet, because the pairs move together. `MAX_TOTAL_RISK_PCT` (default 2.0, so two trades at 1 percent
  each) is the real protection, not the trade count. With the default, a third trade at 1 percent risk is refused. Raise it deliberately.
* If an account somehow has more than one LIVE trade, every one is reconciled and monitored and nothing new opens. If paper and live trades
  are open together, only the live ones are managed until they close.
* The single-position guarantee for live accounts is now enforced in code (the account lock inside one process plus the check in
  `open_live_trade`) rather than by the database alone. A second process on the same database could race the check; `instance_lock` is what
  prevents a second process. UNVERIFIED on the production VPS.

## Owner steps (none done by Claude)

1. Back up the database. Run migration `a1d7c4e9b2f6`. Then deploy the code (switches default to the old behaviour).
2. Before any restart, run `python -m mba_bot.restart_gate` from the app directory: exit 0 means no live trade is open, 3 means do NOT restart,
   2 means the database could not be read. It only reads. It prints counts, no ids.
3. For a paper test account: set `pair_list` in the database, set `MAX_OPEN_POSITIONS=2` or `3`, restart in a quiet moment, watch the journal.
4. Rollback: set `MAX_OPEN_POSITIONS=1` (existing trades are still managed). Downgrading the migration needs every account at one non-terminal trade.
