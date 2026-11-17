# Risk per trade up to 40 percent, and the limits that scale with it (B1c)

Status: ships OFF. `RISK_PCT_MAX` keeps its default 2.0, so nothing changes at deploy; the owner raises it by env, by hand. `ACCOUNT_TOTAL_LOSS_STOP` defaults to 0 (off). Updated 18 Nov 2026: the total stop is a fixed 50% for everyone and the daily limit is the customer's dollar amount. CLAUDE-RUN: tests pass in the sandbox against a FAKE exchange. Nothing is deployed, no migration has been run, no order has been placed. One code path for every account.

## 1. The limit and the range

* `RISK_PCT_MAX` is clamped in config to 0.1 .. 40.0 (`risk_limits.clamp_limit`), whatever the environment says. A customer may choose a risk per trade from 0.1 up to the current `RISK_PCT_MAX`.
* The account API (create and `PATCH /api/account`) REFUSES a value above the limit (or below 0.1) with a 422 and a plain message; it no longer lowers it silently. `clamp_risk_pct` in `risk.py` and the sizing in `trader.py` stay as the last guard. `AccountOut.risk_pct_limit` carries the current limit, and `GET /api/account/risk-limits` carries it before an account exists (onboarding).

## 2. Bands and recorded acknowledgments

| Risk per trade | Band shown | Needs |
|---|---|---|
| 0.1 to 2 (inclusive) | green, text and icon: "Recommended: 2% or less" | nothing |
| above 2 and below 10 | amber, "Higher risk" | acknowledgment 1 |
| 10 and above | red, "Very high risk: a short run of losing trades can remove most of your capital" | acknowledgments 1 and 2 |

The colour is never the only signal (label and icon too). The SERVER enforces it: a request that sets a risk above 2 (or from 10) is refused with a 422 unless the matching acknowledgment is already on file for the current version or is ticked in the same request (it is then recorded). Lowering the number back to 2 or below needs nothing.

Data model: no new table and no migration for this part. The existing `legal_acceptances` table (one row per customer, document type and version, never overwritten) gets two new document types, `risk_ack_high` and `risk_ack_very_high`, version `2026-11-17` (`risk_limits.ACK_VERSION`; bumping it asks everyone again). The row records which acknowledgment, which version and when; the wording the customer ticked is the screen text in `frontend/src/lib/riskBands.js`. Limit: one row per version, so the FIRST acknowledgment is the recorded event; after lowering and raising again no new row is written.

## 3. Daily loss limit: the customer's own dollar amount

The daily limit is no longer a fixed percent. Each live account has its own amount in USDT (`trading_accounts.daily_loss_limit_usd`), set by the customer and required before live trading (see `account_daily_loss.md`). It does not scale with the risk setting, but the screen warns when one trade can lose more than the amount (`risk % x trading capital > amount`): open trades keep their stop-loss, so one trade can still lose more than the limit. The amount must be below the dollar level of the total stop (section 4).

## 4. Total loss stop (`ACCOUNT_TOTAL_LOSS_STOP`, default 0): automatic, one fixed rule

For a LIVE account, when its equity is **50 percent or more below** its REFERENCE equity (`risk_limits.TOTAL_STOP_PCT`, one named constant, the same for every account, not a setting and not dependent on the risk setting), no NEW live entry is opened until the customer resets it on the dashboard with a confirmation. The earlier formula `max(10, 3 x risk %)` capped at 80 is gone. It never resets by itself. Open trades are never touched (their stops and exits keep running). It is evaluated in the same end-of-cycle tick and entry gate as the daily limit, from the same single equity reading per cycle, with a customer-safe alert (once, when it starts), an audit row and the decision-log reason `account_total_loss_stop`. The dashboard shows no always-visible card: the notice and the reset button appear only while the stop is active.

* **Reference equity:** the equity read at the FIRST healthy cycle after `ACCOUNT_TOTAL_LOSS_STOP` is switched on, or after the customer's last reset. Stored on `trading_accounts.loss_reference_equity` (and `loss_reference_at`). It is not the equity at "bot start": the API process does not read the exchange, so the first healthy cycle after the switch (for a new account, the first cycle after Start Bot) is the nearest event the code can see. An account already in a drawdown when the switch is turned on measures from that moment.
* **Fail closed:** an unreadable equity blocks new entries (`account_equity_unreadable`), as the daily limit does. The audit row stores the threshold that applied (50). Its `risk_pct` column is no longer written (stored as 0).
* **Reset:** `POST /api/account/total-loss-reset` with `{"confirm": true}`. It releases an active stop and clears the reference, so the next healthy cycle takes a fresh one from the account's then-current equity. It can also be used with no stop active, to re-baseline.
* **Deposits and withdrawals:** there is no automatic adjustment. A deposit raises equity and can hide a loss from the reference; a withdrawal looks like a loss and can trigger the stop (it fails safe). The reset is the adjustment path: after moving money, the customer resets so the reference is taken again. LIMIT: nothing forces them to.
* **Data (additive migration `c1f4a8d2e6b3`, parent `b9e3d7a2c5f1`):** two nullable columns on `trading_accounts` and the table `account_total_loss_stops` (account, when triggered, reference, equity at hit, loss, threshold, risk, `reset_at`). Idempotent, reversible, no backfill. Not run by Claude.

## 5. Total open risk cap

The cap on the sum of open risk is `max(MAX_TOTAL_RISK_PCT, the account's risk % x the positions it may hold)`, never above 100 (`multi_trade.open_risk_cap`); `MAX_TOTAL_RISK_PCT` of 0 keeps meaning "no cap".

Where it applies today (and only there): (a) `strategy_guards.evaluate`, the portfolio check, whenever a guards mode (`STRATEGY_GUARDS_MODE` for EMA, `NEW_STRATEGY_GUARDS_MODE` for RSI and Donchian) is observe or enforce, for live and paper accounts; positions allowed is 1 for a live account and `MAX_OPEN_POSITIONS` (1 to 3) for a paper account. (b) `trader._multi_paper_limits`, the paper multi-trade entry. Both used to compare against `MAX_TOTAL_RISK_PCT` alone.

What changes: nothing with the guards off and a risk at 2 or below on one slot. With the guards on, a single live trade whose own risk is above `MAX_TOTAL_RISK_PCT` (possible only after `RISK_PCT_MAX` is raised) was refused outright by the old comparison and is now allowed (the cap is at least its own risk). Live stays one position, so for live nothing else changes until multi-position live exists (B7). For paper multi-trade the cap is now never below the nominal sum of the positions allowed, so at the defaults (risk 1, three slots, `MAX_TOTAL_RISK_PCT` 2.0) the third paper trade is no longer refused by this cap (it was); the cap still refuses when open risk is above nominal (a fill worse than planned) and when `MAX_TOTAL_RISK_PCT` is raised above `risk x slots`.

`RSI_MR_RISK_PCT` and `DONCHIAN_RISK_PCT` (0.5) are unchanged.

## 6. Effective limits (`MAX_TOTAL_RISK_PCT` = 2.0 default)

| Risk setting | Daily limit | Total loss stop | Open-risk cap, 1 position | 2 positions | 3 positions |
|---|---|---|---|---|---|
| 1% | set by you | 50% | 2.0% | 2.0% | 3.0% |
| 2% | set by you | 50% | 2.0% | 4.0% | 6.0% |
| 5% | set by you | 50% | 5.0% | 10.0% | 15.0% |
| 10% | set by you | 50% | 10.0% | 20.0% | 30.0% |
| 20% | set by you | 50% | 20.0% | 40.0% | 60.0% |
| 40% | set by you | 50% | 40.0% | 80.0% | 100.0% (120, capped) |

A test computes this table from the code. Note what the numbers mean: the stored risk is a CEILING on each trade's size. Exposure ceilings bind first when the stop is close (Spot cannot exceed 1x the capital, Futures is bound by the margin ceiling and `MAX_NOTIONAL_MULTIPLIER`), so the realised loss at a stop can be well below the nominal risk. It cannot be above it.

## Owner steps (the owner runs every one; nothing here is done by the code)

1. `python -m mba_bot.restart_gate` (exit 0 only; 3 = a live trade is open, do NOT stop; 2 = unreadable, do not stop).
2. Stop the service. 3. Back up the database. 4. `alembic upgrade head` (applies `c1f4a8d2e6b3`). 5. Deploy the code. 6. Start the service; check health and the journal. Nothing changes: `RISK_PCT_MAX` is still 2.0 and the total loss stop is off; the dashboard shows the new risk field with the bands.
7. To allow higher risk (its own restart; gate first): set `RISK_PCT_MAX` to the chosen value (at most 40). To switch on the total loss stop (its own restart): `ACCOUNT_TOTAL_LOSS_STOP=1`; after the restart each live account sets its reference at its first healthy cycle (check `account_total_loss_stops` stays empty and `trading_accounts.loss_reference_equity` fills).

Rollback: set `RISK_PCT_MAX=2.0` and `ACCOUNT_TOTAL_LOSS_STOP=0` (or remove the lines), gate, restart. An account that already holds a risk above the new limit keeps the stored value and is clamped at sizing time (`clamp_risk_pct`); the dashboard asks the customer to lower it when they next save. `alembic downgrade b9e3d7a2c5f1` removes the stop table and the two columns.

## NOT verified

* Any real Binance behaviour; the fake exchange proves control flow only. The equity figures are as verified in B1b (the Futures field against ccxt's source only).
* How a customer experiences the red band and the acknowledgments in a browser: the Vite build compiles and the band rules have node tests, but nobody clicked through the screens.
* The wording of the two acknowledgments is for the owner to approve; the legal pages (Terms, Risk Disclosure) have NOT been updated for risk above 2 percent and need the owner's and a lawyer's decision before the limit is raised.
* The reference-equity definition differs slightly from "at bot start" (see section 4).
* Realised loss at very high risk settings has not been measured on any data.
