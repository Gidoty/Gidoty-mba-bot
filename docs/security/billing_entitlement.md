# Subscription billing, stage 0: the entitlement layer

Status: built behind a switch that is OFF. No provider code, no key, no webhook, no screen. Design: `designs/03` (private handoff repo). Code: `src/mba_bot/billing/entitlement.py`. Tests: `tests/test_entitlement.py`.

## What it is
One function, `is_entitled(customer, settings=..., now=...)`, replaces every read of `Customer.has_paid` that decides who may open NEW live trades: `trader.process_account` (via `onboarding.resolve_trading_mode`), the account API (credentials, enable live trading, start bot, account state) and the customer record the screens read (`/api/auth/me`). A test fails if any other code reads the attribute (the model, the entitlement function and the payment router that writes it are the only exceptions).

## The switch
`SUBSCRIPTION_BILLING_ENABLED` (default off). `SUBSCRIPTION_GRACE_DAYS` (default 5, clamped to 0..60; malformed means 5).

With the switch OFF, `is_entitled` returns exactly `customer.has_paid` and does not read the subscription at all. Nothing else changes. The whole existing suite passes with that single change.

With it ON:

| Customer | Result |
|---|---|
| no subscription row, `has_paid` | entitled for ever (legacy lifetime; owner decision D1) |
| no row, not `has_paid` | not entitled |
| row `active`, now before `current_period_end` | entitled |
| row `active`, from the period end up to the grace end (period end + grace days, or a later stored `grace_until`) | entitled (grace; D2: 5 days) |
| row `active`, at or after the grace end | not entitled |
| row `cancelled`, now before `current_period_end` | entitled, no grace |
| row `cancelled`, at or after it | not entitled |
| any other status, or no `current_period_end` | not entitled (fail closed) |

A row governs when it exists. Times are compared to the second; the period and grace both end AT their end time (exclusive). Naive datetimes from SQLite are read as UTC.

## What expiry does and does not do
It decides NEW entries only (an account that is not entitled resolves to PAPER). An open live position is managed with its own `is_paper` flag to its exit; nothing here closes, cancels or touches a position, a credential or an account, and nothing is deleted. A test opens a protected live trade, expires the subscription and runs a cycle: the trade is unchanged and still protected.

## Data (additive)
Two new tables, `subscriptions` and `billing_payments` (models, an idempotent Alembic revision `f6c3a9d1b8e2` on top of the kill switch one, and tests that it adds only those two tables, backfills nothing and can be run twice). Nothing reads or writes them while the switch is off.
**Deploy note:** `init_db()` runs `create_all()` at every start, so the first start of this code creates the two EMPTY tables by itself. That is additive and touches no existing column. Do not run `alembic upgrade` or `alembic stamp` on the server without the owner's approval and a backup (the usual rule); it is not needed for this to work.

## Rollback
Unset the switch (or set it to 0) and restart: behaviour is exactly `has_paid` again. The tables can stay; code that predates them ignores them. The revision can be downgraded (drops both tables).

## Owner decisions recorded (10 Nov 2026, via the reviewer)
D1 existing paid customers keep lifetime access. D2 grace is 5 days. D4 cards renew automatically (Paystack), other channels get a manual link. D5 an underpayment gets no entitlement and alerts the owner (provider stage). D7 the owner absorbs the NOWPayments fee (floating rate, 7-day window). D3 is a fixed NGN plan price and needs Paystack's WRITTEN answer about price changes before Stage 1. NOWPayments' Subscriptions API is not used (it needs the account password).

## What stage 0 does not do
No payment, no webhook, no renewal, no expiry job, no reminders, no billing screens, no way to create a subscription row except by hand in a test. Stage 1 waits for the reviewer.

## Unverified
- Nothing here was run against a real provider or the production database.
- Enabling the switch before Stage 1 changes nothing for existing customers (they have `has_paid` and no row) but makes every unpaid customer stay unpaid exactly as now; it has no use until a provider stage writes rows.
