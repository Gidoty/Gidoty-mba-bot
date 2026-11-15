# Account-wide daily loss pause and Futures leverage ceiling

Status: both ship OFF. With `ACCOUNT_DAILY_LOSS_PCT=0` (default) and `FUTURES_MAX_LEVERAGE=0` (default) nothing here reads or writes anything. CLAUDE-RUN: tests pass in the sandbox against a FAKE exchange. Nothing is deployed, no migration has been run, no order has been placed.

## 1. Daily loss pause (`ACCOUNT_DAILY_LOSS_PCT`, `ACCOUNT_DAY_TIMEZONE`)

For a LIVE account (paper accounts are not affected). When the account's equity is `ACCOUNT_DAILY_LOSS_PCT` percent or more below the equity it started the local day with, no NEW entry is opened until the end of that day.

* Day: the calendar day in `ACCOUNT_DAY_TIMEZONE` (default `Africa/Lagos`), 00:00 to 23:59 local. Not a rolling 24 hours. The pause ends by itself at the next local midnight and nothing releases it earlier. The per-strategy daily loss guard uses the UTC day and is unchanged; the two boundaries differ by one hour for Lagos.
* Equity (realised plus unrealised): Spot is the total quote balance (free plus locked) plus each open bot position at quantity times mark price; other coins on the account are ignored. Futures is Binance's `totalMarginBalance` (wallet balance plus unrealised profit) from the same balance read. If that field is missing or not a finite number the answer is "unreadable"; it is never rebuilt from other fields.
* Fail closed: an unreadable or malformed equity, or an invalid time zone name (or a server without that tz database), blocks new entries and alerts. It is a refusal, not a recorded loss.
* Day-start equity is stored in `account_day_equity` (one row per account and local day), so a restart mid-day does not reset it. Each cycle of a live account stores a sample. A new day starts from the previous day's last sample if it was taken within one hour before midnight; otherwise from the first read of the day, flagged `approximate`. A day-start never moves up with equity.
* Blocks NEW entries only. Open trades keep their stops and strategy exits (tested: an exit runs during the loss).
* Audit: one row per account and day in `account_loss_pauses` (start equity, equity at hit, loss, limit, zone), one decision-log row per refused signal (`account_daily_loss_pause`, `account_equity_unreadable`, `account_day_timezone_invalid`), one customer alert when the pause starts.
* Known limit: a deposit or withdrawal you make during the day moves equity. A withdrawal looks like a loss (fails safe). A deposit can hide one. The keys are trade-only, so the bot never moves money. There is no adjustment command yet (planned, not built).
* Not a cumulative stop: it resets every day. A cumulative stop from the pilot start is a separate item.

## 2. Futures ceiling (`FUTURES_MAX_LEVERAGE`)

With a value above 0 (the owner sets 5), a new live Futures entry is refused when the leverage Binance reports for the pair is above the value, or the margin mode is not `isolated` (cross, or not reported). The bot only READS leverage and margin mode; it never sets either, and no confidence-based switching exists. The refusal uses a fixed customer message, a decision-log row (`futures_leverage_above_ceiling`, `futures_margin_not_isolated`) and the existing alert de-duplication (`PROTECTION_ALERT_REPEAT_HOURS`, default 6 hours). Spot is never affected.

Interaction with the pre-entry protection check (`PROTECTION_PRECHECK_MODE`): the ceiling runs first and needs no exchange call beyond the leverage read that was already there. The pre-entry check then runs unchanged and judges the stop against the liquidation price. It stays the second line: a leverage under the ceiling can still be refused by it (tested). `PRECHECK_MAX_LEVERAGE` (125) stays a plausibility bound on the value read; it is not the ceiling. For the pilot the owner should set `PROTECTION_PRECHECK_MODE=enforce`; the code default is `observe`, which blocks nothing.

## Owner steps (the owner runs every one; nothing here is done by the code)

1. Check no live trade is open: `python -m mba_bot.restart_gate` (exit 0 = clear; exit 3 = do NOT stop the service; exit 2 = unreadable, do not stop).
2. Stop the service.
3. Back up the database.
4. `alembic upgrade head` (applies `a8d2f6c1e3b7`: two new tables, nothing else changes). It must run before the new code starts, because the new code reads the tables only when the pause is on, but `init_db` and the models expect them.
5. Deploy the code.
6. Start the service. Check health and the journal. Both switches are still off.
7. Enable one switch at a time, each with its own restart (restart gate first, never restart with a live trade open): `ACCOUNT_DAY_TIMEZONE=Africa/Lagos` and `ACCOUNT_DAILY_LOSS_PCT=4`; then `FUTURES_MAX_LEVERAGE=5` (set leverage and isolated margin on Binance by hand first).
8. Watch the decision log for the reason codes above.

Rollback: remove the line, run the restart gate, restart. The downgrade `alembic downgrade f3c8a1d6e2b9` drops the two new tables (the day-start references and pause audit rows are lost; nothing else changes).

## NOT verified

* Binance: nothing was run against it. That ccxt's Futures `fetch_balance` carries `info.totalMarginBalance` as read here is an assumption (the pause blocks entries if it is absent).
* That the VPS has a tz database containing `Africa/Lagos` (the pause blocks entries if not).
* That one extra balance read per live account per cycle fits the Binance request weight budget (not measured).
* Gaps: the pause cannot see a loss that happens between the last sample and the next read, and it does not close anything.
* The fake exchange proves control flow only.
