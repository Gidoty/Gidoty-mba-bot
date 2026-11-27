# The USDT payment panel (NOWPayments)

What the customer sees while paying in USDT, and what the server does when they say they have paid. Nothing here changes how access is granted: the one apply rule (`nowpayments.process_record`, the code the IPN always ran) decides, as before.

## Why the countdown restarted by itself

The countdown the customer saw was not ours. Before this change the app had no countdown for a USDT payment at all (the only timer in the billing screens is the Paystack return poll): the USDT button called `POST /api/billing/crypto/initialize` and sent the browser to the invoice page on nowpayments.io. That page is NOWPayments' own. Our code creates the invoice with a 7-day window, and each payment made on an invoice has its own 20-minute window. The hosted invoice page makes a new payment (a new address and amount, a fresh 20 minutes) when the previous payment window runs out, so its countdown starts again. That is how the code reads; I could not open NOWPayments' page from here, so treat it as unverified until the owner has seen the page do it again.

## What changed

- **A panel in our own page** replaces the same-tab redirect for USDT. It shows the countdown to a FIXED end time (the invoice's creation time plus 20 minutes, from the server), so it can only count down: at 0 it says "This invoice expired" and offers "Create a new invoice". It never starts again by itself. A payment found after the end shows a message with the invoice reference for support.
- **Stages** (plain words): waiting for your transfer; transfer seen and confirming; payment confirmed; underpaid (with how much is short, in USD); expired; failed; refunded.
- **`GET /api/billing/crypto/status`** reads our own database only (no call to NOWPayments, no write). The panel asks every 10 seconds. 404 while subscription billing or NOWPayments is off.
- **`POST /api/billing/crypto/check`** ("I have sent the payment"): grants nothing by itself. The server asks NOWPayments for the current record of the invoice's payment (existing client and key) and passes it through the same apply rule as the IPN. `NOWPAYMENTS_IPN_MODE`: off answers 404; observe writes nothing and answers only with the status; enforce applies as the IPN would. The invoice must be the logged-in customer's own (anything else is a plain 404). Limits: one call per customer per 15 seconds, 20 per invoice (in this process's memory, a restart clears them).
- **One apply rule**: the IPN handler's second half is now `process_record`, called by the IPN and by the check. A payment record that belongs to another customer's invoice is ignored by the check.

## Two small changes to what is stored (no migration)

- A pending IPN (`waiting`, `confirming`, `confirmed`, `sending`) now writes one `payment_events` row (`seen:<status>`) and the payment id on the invoice. Before, it wrote nothing. It still grants nothing, the invoice stays `pending`, and replays are ignored by the existing unique key. This is what lets the page say "transfer seen and confirming" from our database.
- A `partially_paid` payment now carries its USD value when the record has the amounts, so the page can say how much is short.

## Limits

- In observe mode nothing is written, so the page cannot learn the payment from our database; the button works because the payment id is remembered in memory when an observed IPN arrives (lost on restart; then the button answers "no payment seen yet").
- NOWPayments' real answers are unverified (as in `docs/security/billing_nowpayments.md`).
- The invoice link is kept only in the browser's page state: after a reload the panel reopens (from our database) but without the "Open the payment page" button; "Create a new invoice" gives a fresh link. At most 3 unpaid invoices a day (the existing limit).
- The 20-minute window is the length the owner saw, not a value read from the provider.
