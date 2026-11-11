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

---

# Follow-up, 11 Nov 2026: real bodies, more events, customer reminders

Everything below is behind `SUBSCRIPTION_BILLING_ENABLED` (default OFF), like Stage 1. With it off: the new banner route answers
`{"notice": null}` without reading anything, the reminder job does nothing, the new events fall through to the existing handler,
and no new table is touched.

## 1. Tests against real bodies
`tests/fixtures/paystack/` holds real Paystack TEST-mode bodies from the 11 Nov end-to-end run (account and object identifiers
replaced, see its README). `tests/test_paystack_real_bodies.py` feeds them through the handlers and asserts the outcomes the live run
produced: first payment (in both live runs `subscription.create` arrived BEFORE `charge.success`), replay, a renewal, `invoice.create`
and a cancel; plus the whole hourly sequence through the real route with a frozen clock (every answer 200, no 503).

Where the real bodies differed from a naive reading (all already read correctly; now pinned by tests):
* A renewal `charge.success` has `metadata = {"invoice_action": "create"}` and NO customer id. It is tied to the customer by e-mail, the
  plan code and a verified payment. A charge body names no subscription code (subscription events do).
* The webhook's `plan` is an object; the verify answer's `plan` is a plain string (the object is `plan_object`). `event_plan_code` reads both.
* `invoice.create` carries the transaction reference nested in `data.transaction.reference` and the subscription in `data.subscription`.
* The renewal was charged price plus fee, same as the first payment (1,532,995 kobo for a 1,500,000 plan).
No parser change was needed. Not captured, so still invented bodies: `invoice.payment_failed`, `subscription.disable`,
`subscription.expiring_cards`, `refund.*`.

## 2. Events
| Event | What happens |
|---|---|
| `invoice.create`, `invoice.update` | Ignored (answered 200, nothing read). The payment is handled from `charge.success`. |
| `invoice.payment_failed` | As before (nothing extended, owner alerted, grace starts when the period ends) plus a notice to the customer (section 3). |
| `subscription.expiring_cards` | A reminder to the customer, once per period. Body shape UNVERIFIED: read as a list or one object, each item tied to a customer by subscription code or e-mail. |
| `refund.pending`, `refund.processing`, `refund.processed`, `refund.failed` | All covered by the `refund.` prefix. The owner is alerted. A payment is marked refunded only if Paystack's own transaction record says `reversed`; `reversal pending` and `success` do not mark it. `refund.failed` only alerts. Access is never revoked. |

## 3. Customer reminders (start of Stage 3)
Paystack does not retry a failed renewal; it e-mails the customer a manual link. So:
* **Renewal failed:** a Telegram message (if the customer linked Telegram) and an in-app banner with a "Pay now" button, once per failed period
  (`billing_notices` has a unique key on customer, kind and period). Wording says access continues for the 5-day grace period (the number is `SUBSCRIPTION_GRACE_DAYS`) and gives the date.
  "Pay now" calls `POST /api/billing/subscription/initialize` again, which is now allowed from the grace window onwards (still refused while the paid period runs).
* **Card expiring:** reminder, once per period.
* **3 days before the period end:** for active subscriptions whose provider is not `paystack` (a manual grant, or later crypto); card subscriptions renew by themselves.
  The job runs from the scheduler at most once an hour, after the trading round, and every failure is caught. No such subscription exists yet, and no non-card payment link exists until Stage 2, so the banner has no button for it.
* **Cancelled (banner only):** "Cancelling stops the next charge. Your access continues until the end of the month you have already paid for."
* All texts are fixed strings (no provider text, e-mail or amount). Telegram sends happen after the commit, so no SQLite write lock is held while waiting, and a failed send changes nothing else.

**Subscribing again after a failed renewal.** Initialize creates a NEW Paystack subscription while the old one is still `attention`. When the new payment is verified and the new
subscription is applied, the old subscription is cancelled (`POST /subscription/disable` with its own stored token), so the customer is not charged by both. Only a failed (`attention`) or due-and-unpaid subscription is ever cancelled; one whose next payment date is still ahead never is. If that call fails, the owner is alerted to cancel it in Paystack. This is the only write this application sends to Paystack.

## 4. OPEN RISK: paying through Paystack's manual retry link (UNKNOWN, do not guess)
Nobody has seen what Paystack does here. What the code does TODAY (pinned in `tests/test_billing_notices.py`, "retry_link" tests):

| What Paystack does (assumed) | What our code does |
|---|---|
| A. Moves `next_payment_date` forward and sends `charge.success` with its own reference and no customer id | Tied to the customer by e-mail and plan, verified, applied. Period end becomes Paystack's new date. Works whether the status is back to `active` or still `attention`. |
| B. Sends `charge.success` but leaves `next_payment_date` at the old, passed date | NOT applied: the payment is verified, but we will not invent a period end. Answer 503 (Paystack redelivers for up to 72 hours), nothing stored for the payment. The owner is alerted once per 6 hours once the payment is over 10 minutes old (added in this change: before it the failure was silent). The customer gets no extra time; access ends with the grace window. Owner action needed. |
| C. Sends a different event only (for example `invoice.update`) and no `charge.success` | `invoice.update` is ignored, so nothing is applied. Same customer outcome as B, and no alert (we never see a payment). |
| D. Pays the invoice but a new subscription is created | Handled like "subscribing again": the new one is applied if it is `active` and the old `attention` one cancelled. |

Still unknown: whether `next_payment_date` advances; which events arrive and with which reference; whether the status returns to `active`; whether `invoice.update` carries the paid status.
There is no tool yet for the owner to extend a customer by hand (case B); that is a Stage 3 item.
Proposed owner test (NOT run, needs the owner's yes): an hourly TEST plan, a test customer whose e-mail is an inbox the owner can read, update the card on the subscription to the declined test card through the manage link
(UNVERIFIED that test mode allows it), wait for the failed renewal, pay through the e-mailed link with a good card, and capture the events, the reference and `next_payment_date` with the e2e launcher.

## 5. Deploy notes
* Migration `b8e5f2a0c4d6` (additive, idempotent): table `billing_notices`. Not run anywhere by this change.
* New setting use: `BILLING_ALERT_CHAT_ID` (owner alerts) and, for customer messages, the existing `TELEGRAM_BOT_TOKEN`.
* The frontend (served from Vercel) now calls `GET /api/billing/notice` on every authenticated page. A frontend built from this change that meets a backend without the route simply shows nothing (the call is caught).
* Terms: Section 11 gained one sentence ("Where access is sold as a subscription, cancelling stops the next charge, and your access continues until the end of the period you have already paid for"). `TERMS_VERSION` was NOT bumped (a bump forces every customer to accept again before using the account routes). That is a decision for the owner.
