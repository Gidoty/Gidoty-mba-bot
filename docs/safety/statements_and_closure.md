# Customer statements (CSV) and closing an account

Status: built. The statement download is a read-only route for the signed-in customer (always available once deployed; it writes nothing). The deletion flow is OFF: it needs `ADMIN_ACTIONS_ENABLED` and `ACCOUNT_DELETION_ENABLED`. **Depends on the operator-settings and admin-actions PR** (branch `feature/settings-table-admin-actions`): the deletion route reuses its admin step-up, reason and change-log code. The statement part depends on nothing. **No migration**: no table or column is added.

## Trade statement
`GET /api/statements/trades.csv` (optional `mode=live|demo|paper`, `from=YYYY-MM-DD`, `to=YYYY-MM-DD`, closing time, from inclusive and to exclusive). The Account page has a "Download statement (CSV)" button. One row per CLOSED trade of the signed-in customer's own account, at most 20,000 rows (`X-Statement-Truncated: 1` says it was cut).

| column | filled when |
|---|---|
| gross_pnl | the trade has a recorded result (`trades.pnl`: before fees and before funding) |
| entry_fee, exit_fee, fees | both real commissions were captured from the fills, and the trade is not a practice trade |
| funding | **never today**: Futures funding is not recorded anywhere on main |
| net_pnl | gross and both fees are known (gross minus fees). Funding is NOT in it |
| note | says what is missing: "fees not captured for this trade", "practice trade: simulated, no fees or slippage", "funding is not recorded" |

**Net P&L work (#98):** when it is merged and records funding and a net figure per trade, add its columns here; until then funding stays empty and net is gross minus fees only. Nothing is estimated. Text cells that start with `=`, `+`, `-`, `@`, tab or return get a leading apostrophe so a spreadsheet cannot run them as a formula; numbers are plain numbers (a loss is written `-3.5`).

## Closing an account and deleting the data
1. The customer asks from the Account page (existing `POST /api/privacy/requests`, kind `deletion`).
2. An admin approves it in the dashboard (`requests/{id}/approve`, operator-settings PR). Nothing is erased yet.
3. An admin completes it: `POST /api/admin/requests/{id}/complete-deletion {reason, code, confirm}` (dashboard action "Complete an approved deletion"). Needs both switches, the admin sign-in, a fresh authenticator code, a reason, and the exact text `DELETE <first 8 characters of the customer id>`.

Refused (nothing changes) unless: the request is a deletion and already approved (not open, not done); no trade of the account is still open (a live position is never abandoned: close it first); the customer is not an admin. It then, in ONE transaction, erases every row that holds the customer's or the account's data and anonymises the customer row (email becomes `deleted-<id>@deleted.invalid`, the password becomes an unusable random one, the account is inactive, the old session stops working) and marks the request `done`. It places, closes or changes no trade or order and calls no exchange or payment provider. It writes an `admin_changes` row (who, when, counts, reason; no email).

**Erased:** the trading account and its settings, exchange and demo credentials, trades, telemetry and decision logs, pair skips, equity and loss records, strategy pauses, alert marks, admin notes, the provider's subscription email token, and (by default) any table added later that holds a customer or account id: the list is derived from the schema, and a test fails if a table holding such an id would be kept by accident.

**Kept, and why** (the same three lines as `privacy.RETAINED_RECORDS`, which the Privacy Policy repeats; a test checks they agree): payment and subscription records (`billing_payments`, `payment_events`, `payment_references`, `subscriptions`, `billing_notices`: to reconcile payments, handle refunds and disputes, and meet accounting duties); the customer's acceptances of the Terms, Privacy Policy and Risk Disclosure with version and time; the data request record and its outcome. These rows reference the customer id, so the customer row stays as an anonymised shell with no email, no password and no key. The admin audit and change logs stay (they hold the admin's id and the customer's pseudonymous id, never an email).

**The Privacy text was NOT changed.** It already lists these three kinds of record. Whether it should also say that an anonymised shell of the account remains, and how long the payment records are kept, is a decision for the owner or a lawyer; changing the text means a new version and every customer accepting it again, so it was left alone.

**Not done by this tool:** cancelling a subscription at Paystack or NOWPayments (an active subscription row produces a warning; do it at the provider), deleting backups (a deleted customer remains in older database backups until those expire; the policy for how long is the owner's), and anything held by Telegram, Binance or the payment providers themselves.

## Owner steps (nothing here was run by Claude)
1. Deploy when no live trade is open. No migration.
2. To use the deletion flow: `ADMIN_ACTIONS_ENABLED=true` and `ACCOUNT_DELETION_ENABLED=true`, restart. Take a database backup first (`ops/deploy/backup_check.sh` if that PR is merged): the erasure cannot be undone from the dashboard.
3. Rollback: unset `ACCOUNT_DELETION_ENABLED` and restart. Erased rows stay erased.

## Not proven
Fake data in an isolated database only. No real customer, backup or provider was involved. Whether the retained records satisfy a particular country's law is not something a test can show.
