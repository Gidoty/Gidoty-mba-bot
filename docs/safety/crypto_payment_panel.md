# The USDT payment panel (NOWPayments)

What the customer sees while paying in USDT, and what the server does when they say they have paid. Nothing here changes how access is granted: the one apply rule (`nowpayments.process_record`, the code the IPN always ran) decides, as before.

## Why the countdown restarted by itself

The countdown the customer saw was not ours. Before this change the app had no countdown for a USDT payment at all (the only timer in the billing screens is the Paystack return poll): the USDT button called `POST /api/billing/crypto/initialize` and sent the browser to the invoice page on nowpayments.io. That page is NOWPayments' own. Our code creates the invoice with a 7-day window, and each payment made on an invoice has its own 20-minute window. The hosted invoice page makes a new payment (a new address and amount, a fresh 20 minutes) when the previous payment window runs out, so its countdown starts again. That is how the code reads; I could not open NOWPayments' page from here, so treat it as unverified until the owner has seen the page do it again.

## What changed

- **A panel in our own page** replaces the same-tab redirect for USDT. It shows the countdown to a FIXED end time (the invoice's creation time plus 20 minutes, from the server), so it can only count down: at 0 it says "This invoice expired" and offers "Create a new invoice". It never starts again by itself. A payment found after the end shows a message with the invoice reference for support.
- **Stages** (plain words): waiting for your transfer; transfer seen and confirming; payment confirmed; underpaid (with how much is short, in USD); expired; failed; refunded; wrong coin or network.
- **What to send.** While waiting, the panel names the exact coin and network the invoice asked for ("USDT on BSC (BEP20)"), the exact amount once the provider's payment record exists (before that: the price in USD and "the exact amount is shown on the payment page"), and says that other coins or networks are not credited automatically.
- **`GET /api/billing/crypto/status`** reads our own database only (no call to NOWPayments, no write). The panel asks every 10 seconds. 404 while subscription billing or NOWPayments is off.
- **"I have sent the payment" is withheld.** It was built (a check route that asks NOWPayments and passes the record through the same apply rule as the IPN, with ownership, rate limits and an observe-mode no-write rule) and then taken out of this change until the owner has read the written report on how the parent payment can be found. It is one commit that can be reverted to bring it back, with its tests.
- **One apply rule**: the IPN handler's second half is `process_record`, the one function that decides what a payment record means.

## Wrong coin or network

The live test showed it: the invoice asked for USDT on BSC, the customer sent another coin, NOWPayments converted it and created a CHILD payment linked to the invoice's own (parent) payment. The child cannot be read with our API key (HTTP 404), so a verified notification for it was logged and dropped.

- A verified notification whose payment cannot be read, or whose payment record is in another coin than `NOWPAYMENTS_PAY_CURRENCY`, is matched to our invoice using only what the notification carries: the order id, else the parent payment id (stored on the invoice from the parent's own notice, or remembered in this process), and, for an unreadable payment, the coin it names. No amount from the notification is ever used, and no access is ever granted.
- **enforce:** one `payment_events` row (`wrong_coin`, one per invoice, so a second notification changes nothing) and one owner alert per invoice with a masked order tail ("Order ...abc123"). The invoice stays `pending` (no new status value, no migration). The page shows the stage "wrong coin or network": "We received a payment in a different coin or network. It was not credited automatically. Contact support and quote reference X."
- **observe:** nothing is written and no owner alert is sent. The invoice is remembered in this process's memory only, so the page shows the stage until the next restart. After a restart the notification is not replayed, so the stage is gone.
- The stage is sticky: later "waiting" notices of the parent payment do not hide it. A later payment in the right coin that finishes is applied as always.
- An unreadable payment that names no invoice of ours, or one in the right coin with no parent, is ignored as before.

## Two small changes to what is stored (no migration)

- A pending IPN (`waiting`, `confirming`, `confirmed`, `sending`) now writes one `payment_events` row (`seen:<status>`, with the exact coin amount of the payment) and the payment id on the invoice. Before, it wrote nothing. It still grants nothing, the invoice stays `pending`, and replays are ignored by the existing unique key. This is what lets the page say "transfer seen and confirming" from our database.
- A `partially_paid` payment now carries its USD value when the record has the amounts, so the page can say how much is short.

## Limits

- In observe mode nothing is written, so the page cannot learn about a payment from our database; only the wrong-coin stage and the exact amount are kept, in memory, until a restart.
- NOWPayments' real answers are unverified (as in `docs/security/billing_nowpayments.md`).
- The invoice link is kept only in the browser's page state: after a reload the panel reopens (from our database) but without the "Open the payment page" button; "Create a new invoice" gives a fresh link. At most 3 unpaid invoices a day (the existing limit).
- The 20-minute window is the length the owner saw, not a value read from the provider.
