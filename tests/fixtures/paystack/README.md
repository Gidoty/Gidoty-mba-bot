# Paystack webhook and API fixtures

Real Paystack TEST-mode bodies from an end-to-end run of the subscription flow on 11 Nov 2026: the webhook bodies as Paystack
sent them (`events`) and the answers Paystack gave to the reads the handlers made while processing them (`reads`, in the order
they happened). `monthly.json` is a monthly plan (first payment, cancel). `hourly.json` is an hourly plan (first payment,
renewal, `invoice.create`, cancel).

What was changed, and nothing else:
* The capture tool had already masked e-mail addresses, tokens, card fields and signatures (`<masked:str>`).
* Account and object identifiers (integration id, customer ids and codes, subscription, plan and invoice codes and ids,
  transaction ids and references) were replaced with fixed stand-ins, consistently, so that references between objects still match.
* `{EMAIL}` and `{CUSTOMER_ID}` are placeholders that `tests/test_paystack_real_bodies.py` fills in with the test customer.
* Dates, amounts, statuses, field names and structure are exactly as received.

No key, token, webhook signature or real e-mail address is in these files. Not captured, so NOT covered by real bodies:
`invoice.payment_failed`, `subscription.disable`, `subscription.expiring_cards`, `refund.*`.
