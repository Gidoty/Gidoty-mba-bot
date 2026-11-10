# Paystack subscriptions (designs/03, Stage 1)

Status: BUILT, tested offline against a fake Paystack, NOT deployed, NOT run against Paystack. Everything is behind
`SUBSCRIPTION_BILLING_ENABLED` (default OFF). With it off: the new route answers 404, the webhook runs exactly the code it ran
before, no new table is read or written, `/api/billing/initialize` and the NGN 135,000 activation are unchanged.

## Switches and settings (all read only while the switch is on)
| Variable | Default | Meaning |
|---|---|---|
| SUBSCRIPTION_BILLING_ENABLED | off | Master switch (Stage 0 and Stage 1). |
| PAYSTACK_PLAN_CODE | unset | Code of the monthly NGN plan the owner creates in the Paystack dashboard. Unset = checkout answers 503. |
| PAYSTACK_PLAN_AMOUNT | 1500000 | The plan price in kobo (NGN 15,000.00). Used as the price a first payment is checked against. |
| BILLING_ALERT_CHAT_ID | unset | Telegram chat for owner alerts. Unset = alerts go to the log only. Needs TELEGRAM_BOT_TOKEN. |
| SUBSCRIPTION_GRACE_DAYS | 5 | Stage 0. |

Rollback: unset SUBSCRIPTION_BILLING_ENABLED and restart. Subscription rows stay in the database and are ignored; customers who paid
only through a subscription would then be judged by `has_paid` (false), so roll back only before any customer subscribes, or
after the owner has decided how to treat them.

## Flow
1. `POST /api/billing/subscription/initialize` (logged-in customer without lifetime access or an active subscription): records a
   pending `billing_payments` row (reference `mbasub_...`, price 1,500,000 NGN) and calls Paystack `transaction/initialize` with the
   plan code. No amount and no currency are sent, so the plan decides the price. HTTP 200 and 201 are both success.
2. Webhook (`/api/billing/webhook`, signature checked on the raw bytes first). Events handled: `charge.success` (only when it is
   one of ours), `subscription.create`, `subscription.not_renew`, `subscription.disable`, `invoice.payment_failed`, `refund.*`.
   Any other event, and any `charge.success` that is not ours, goes to the existing handler unchanged.
3. The body is a trigger only. The handler re-reads `GET /transaction/verify/:reference` and `GET /subscription/:code` (and, to find
   a new subscription, the plan id, the Paystack customer, and `GET /subscription?plan=<numeric id>&customer=<id>`).

## The ladder every payment passes
signature (raw bytes) -> verify by API -> status success -> reference equals the one issued (or a renewal tied to a customer who
has our subscription) -> currency NGN -> amount via the existing `amount_problem` (price, or price + Paystack's own fee, nothing
else; renewals are checked against the price of the customer's last verified payment) -> customer match (metadata id and/or
email; at least one positive, none contradicting) -> plan match -> replay protection (`payment_events` row `sub-charge:<ref>`
plus the unique `billing_payments` reference). Any failure grants nothing, marks the payment `failed` or `underpaid`, and alerts.

## Period end
`current_period_end` = Paystack's `next_payment_date` from the re-read subscription. The code does no month arithmetic (a test
checks the source). A renewal is applied only if that date is later than the current end; sanity bound: not more than 45 days
ahead. If Paystack's record has not caught up, or the date is missing or out of range, nothing is written and the webhook
answers 503 so Paystack delivers it again.

## States
Paystack `active` and `attention` -> ours `active`. `non-renewing` -> `cancelled` (entitled until the kept date, then not entitled;
Stage 0 gives a cancelled subscription no grace). `completed` and `cancelled` -> `cancelled` with the period end untouched.
Unknown status -> nothing changes, owner alerted. Failed renewal: nothing changes, owner alerted; the paid period ending starts the
5-day grace window by itself. Refund: payment marked `refunded` only if Paystack's record says `reversed` (UNVERIFIED wording),
owner alerted, access never revoked automatically.

## Data
New table `subscription_tokens` (migration a7d4e1c9b3f5, additive, idempotent): the Paystack subscription email token, encrypted with
ENCRYPTION_KEY. It is never logged, never returned and never put in a message. `subscriptions` and `billing_payments` come from Stage 0.

## Concurrency
SQLite allows one writer. Every Paystack read happens with no write pending; writes then happen with no await inside; the
session is committed before alerts are sent. A test holds a second SQLite connection open during every Paystack read and checks it
can take the write lock.

## What the offline tests cannot prove (UNVERIFIED)
* The real webhook bodies. None were captured; every body in the tests is invented and handlers read many alternative field names.
* Whether a real renewal charge carries Paystack's fee. Both shapes pass; any other amount is refused and alerted.
* How quickly Paystack's subscription record follows a payment, and its retry schedule for a 503 (documented as every 3 minutes four times, then hourly up to 72 hours; the docs also contradict themselves on retries).
* The wording of a refunded transaction's status.
* That the customer fetch (`GET /customer/:email`) and the plan fetch return the `id` fields assumed here.
* Response time: the handler waits for up to two or three Paystack reads before answering (8 s limit each); the reviewer asked for a fast 200.
* Anything about live mode, the fee at NGN 15,000 (about NGN 15,329 is inferred) and real bank or card behaviour.
