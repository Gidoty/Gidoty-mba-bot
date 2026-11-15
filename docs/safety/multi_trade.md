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

One trade may use at most 1/slots of the paper balance as notional when `MAX_OPEN_POSITIONS` is above 1 (a third for three slots, a half for two; owner decision, audit L9), the first trade included; with one slot nothing changes. Slot free for the grade; total open risk (money between entry and stop, from the stored trades) plus the new trade within
`MAX_TOTAL_RISK_PCT`; free capital (balance minus the notional of open trades, counted at 1x) above zero, with the quantity trimmed to
what is free; notional at least `PAPER_MIN_NOTIONAL`. A refusal is logged with a reason code (`trade_slots_full`, `total_open_risk_cap`,
`free_capital_short`) and the decision log when telemetry is on. Apart from the notional share above, the first trade of an account is never touched by these checks.
`PAPER_MIN_NOTIONAL` is a stand-in: paper has no exchange, so the real Binance minimum is not read. UNVERIFIED.

## Database

* `trades`: the unique index `uq_trades_one_nonterminal_per_account` is replaced by `uq_trades_one_nonterminal_per_pair` (account, pair) plus
  `uq_trades_one_nonterminal_no_pair` (account, only for rows with no pair, the old rule for legacy rows).
* New nullable columns: `trades.strategy_id`, `trading_accounts.pair_list`.
* Migration `e7a2c9d4b1f8` (parent `a1d7c4e9b2f6`): `uq_trades_one_live_nonterminal_per_account` on `trades (account_id)` where the status is non-terminal AND the row is a live trade.
  Paper rows are outside it. The upgrade REFUSES (changing nothing) if an account already has two non-terminal live trades. Reversible: the downgrade drops the index. Not run by Claude.
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
* Auto Select is ignored for an account that uses a pair list and for an account the owner opted in to a new strategy (`extra_strategies`); other paper accounts keep it.
* Guard statistics (loss streak, daily loss, expectancy) are kept per strategy since the strategy PRs (audit L10; a row with no strategy id is the EMA strategy's) and charged each strategy's own fee and slippage per market. The stop cooldown and the RSI two-loss pause are per pair. The market grade still pools all of the account's paper trades, each at its own strategy's costs.
* Three crypto trades at once are often one bet, because the pairs move together. `MAX_TOTAL_RISK_PCT` (default 2.0, so two trades at 1 percent
  each) is the real protection, not the trade count. With the default, a third trade at 1 percent risk is refused. Raise it deliberately.
* If an account somehow has more than one LIVE trade, every one is reconciled and monitored and nothing new opens. If paper and live trades
  are open together, only the live ones are managed until they close.
* The one-live-trade rule is enforced by the DATABASE again (migration `e7a2c9d4b1f8`, partial unique index `uq_trades_one_live_nonterminal_per_account` on
  non-terminal LIVE rows, whatever the pair; independent audit finding B1), and also in code (the account lock inside one process plus the check in
  `open_live_trade`). Two scheduler processes can therefore no longer both open a live trade. **The owner should still set `API_SINGLE_INSTANCE_LOCK=true`**
  (the default stays off; this stack does not change it) so that a second scheduler process refuses to start at all. UNVERIFIED on the production VPS: whether it is set, and how many workers run.

## Owner steps (none done by Claude)

Deploy order (audit M7). Either shortcut leaves a window: migrating while the OLD code runs puts the new schema under old code (the old live-trade check is not in the new schema's index set until the chain is complete,
and the old code does not know the new columns' meaning); deploying the new code on the OLD schema breaks every trade query. So, with the service STOPPED for the few minutes it takes:

1. Check that no live trade is open: `python -m mba_bot.restart_gate` from the app directory with the service's `DATABASE_URL`. Exit 0 means none, 3 means do NOT stop the service, 2 means the database could not be read (a missing file or table counts).
   It only reads, opens its own connection, creates nothing, and prints counts, no ids.
2. Stop the service.
3. Back up the database (and check the backup).
4. Migrate: `alembic upgrade head` from the app directory with the service's `DATABASE_URL`. This applies, in order, `a1d7c4e9b2f6` (columns, per-pair index), `e7a2c9d4b1f8` (the one-live-trade-per-account index; it refuses if an account already has
   two non-terminal live trades) and `f3c8a1d6e2b9` (the per-account opt-in column). On the way it also applies any earlier pending revision.
5. Deploy the code (switches default to the old behaviour; nothing else changes until you set a switch).
6. Start the service and check the health endpoint and the journal. Set `API_SINGLE_INSTANCE_LOCK=true` in the same environment change if it is not set yet (this stack does not change the default).
7. For a paper test account: set `pair_list` in the database (and, for the new strategies, `extra_strategies`; see rsi_mr_v1.md and donchian_breakout_v1.md), set `MAX_OPEN_POSITIONS=2` or `3`, restart in a quiet moment, watch the journal.
8. Rollback: set `MAX_OPEN_POSITIONS=1` (existing trades are still managed). Downgrading the migrations needs every account at one non-terminal trade (the `a1d7c4e9b2f6` downgrade refuses otherwise).
   **What a database downgrade loses** (audit M6): `trades.strategy_id` (an open rsi_mr_v1 or donchian_breakout_v1 paper trade is then relabelled as ema_cross_atr_v1 after a later re-upgrade and is exited by the EMA rules; close such trades first),
   `trading_accounts.pair_list` and `trading_accounts.extra_strategies` (every account is EMA only with one pair), and the live-trade and per-pair indexes (back to one trade per account). The `c9f6a3b1d5e7` downgrade, which would also drop all strategy pauses,
   refuses while an active pause exists (see strategy_guards.md). Prefer switching the features off (`MAX_OPEN_POSITIONS=1`, the strategy switches off) over downgrading.

## Noted by the independent audit, NOT changed (docs note only)

* **L3 (`risk.py`, existing code):** `compute_stop_loss` and `compute_position_size` pass a NaN through, and the `quantity <= 0` refusal does not fire on NaN; `rsi_mr.signal_from_values` would return "long" for a NaN close. Binance does not send NaN candles in practice. Not touched in this stack.
* **L5 (`a1d7c4e9b2f6` downgrade):** it uses a plain `drop_column`, which needs SQLite 3.35 or newer. It worked on 3.53 in the audit; the SQLite version on the server was not checked.
* **L7 (daily loss limit):** `STRATEGY_DAILY_LOSS_PCT` is applied per strategy, so with three strategies an account can lose up to three times that percentage in one UTC day. The customer message for a daily-loss refusal says "The daily loss limit was reached", which is true for the strategy that hit it, not necessarily for the account.
