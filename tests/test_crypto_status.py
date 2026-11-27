"""The customer's USDT panel, server side (docs/safety/crypto_payment_panel.md): GET /api/billing/crypto/status (our own database only).

PROVES (fake NOWPayments, scratch SQLite): the status route is gated by the switches, reads only our database and says plain stages; an underpaid payment says how much is short; a payment seen after the window is
flagged; an invoice that never got a link is skipped. (The "I have sent the payment" route and its tests are withheld until the owner has read the report: see the PR.)
DOES NOT PROVE: the real shape of NOWPayments' answers (UNVERIFIED, as in test_nowpayments.py) or anything in a browser."""
from __future__ import annotations

from datetime import timedelta

import pytest
from test_nowpayments import U, app, bp_state, count, customer, entitled, fake, ipn, issue, pay, record, sub_row, tg, trigger  # noqa: F401
from test_security_hardening import make_app  # noqa: F401

from mba_bot.billing import nowpayments as npay
from mba_bot.db import session_scope
from mba_bot.models import BillingPayment, PaymentEvent, Subscription

REF = "mbacrypto_ref1"



@pytest.fixture(autouse=True)
def fresh_memory():
    npay.reset_seen_for_tests()
    yield
    npay.reset_seen_for_tests()


def status(c):
    r = c.get("/api/billing/crypto/status")
    assert r.status_code == 200, r.text
    return r.json()


# ====================================================================== gating
def test_the_status_route_answers_404_while_billing_or_nowpayments_is_off_and_401_without_a_login(app, fake):
    with app() as c:
        assert c.get("/api/billing/crypto/status").status_code == 401
    with app(NOWPAYMENTS_ENABLED="0") as c:
        customer(c, "e1@example.com")
        assert c.get("/api/billing/crypto/status").status_code == 404
    with app(SUBSCRIPTION_BILLING_ENABLED="0") as c:
        customer(c, "e2@example.com")
        assert c.get("/api/billing/crypto/status").status_code == 404


def test_the_check_route_is_withheld(app, fake):
    with app() as c:
        customer(c, "w@example.com")
        assert c.post("/api/billing/crypto/check", json={}).status_code in (404, 405)




# ====================================================================== the status route (database only)
def test_the_status_route_reads_only_our_database(app, fake):
    with app() as c:
        cid = customer(c)
        assert status(c)["invoice"] is None
        issue(cid)
        s = status(c)
        assert s["invoice"]["stage"] == "waiting" and s["invoice"]["reference"] == REF and s["invoice"]["price_usd"] == "10.00" and s["window_minutes"] == 20
        assert fake.calls == [] and fake.posts == []


def test_the_window_is_twenty_minutes_from_the_invoice_and_the_server_time_is_sent(app, fake):
    from datetime import datetime

    with app() as c:
        cid = customer(c)
        issue(cid)
        inv = status(c)
        issued = datetime.fromisoformat(inv["invoice"]["issued_at"])
        ends = datetime.fromisoformat(inv["invoice"]["window_ends_at"])
        assert ends - issued == timedelta(minutes=20) and abs((datetime.fromisoformat(inv["server_time"]) - datetime.now(U)).total_seconds()) < 30


@pytest.mark.parametrize("pay_status,stage", [("waiting", "waiting"), ("confirming", "confirming"), ("confirmed", "confirming"), ("sending", "confirming"), ("finished", "confirmed"), ("expired", "expired"), ("failed", "failed")])
def test_each_ipn_status_shows_as_a_plain_stage_from_our_database(app, fake, pay_status, stage):
    with app() as c:
        cid = customer(c)
        assert pay(c, fake, cid, status=pay_status).status_code == 200
        assert status(c)["invoice"]["stage"] == stage and fake.calls == ["/v1/payment/5077125051"]  # the one call is the IPN's own read; the status route made none


def test_an_underpaid_payment_says_how_much_is_short(app, fake):
    with app() as c:
        cid = customer(c)
        pay(c, fake, cid, status="partially_paid", pay_amount=10.0, actually_paid=6.0)
        inv = status(c)["invoice"]
        assert inv["stage"] == "underpaid" and inv["short_usd"] == "4.00"
    with app() as c:
        cid = customer(c, "other@example.com")
        pay(c, fake, cid, order="mbacrypto_ref2", pid=7001, pay_amount=10.0, actually_paid=9.0)  # finished but below the tolerance
        assert status(c)["invoice"]["stage"] == "underpaid" and status(c)["invoice"]["short_usd"] == "1.00"


def test_a_payment_first_seen_after_the_window_is_flagged_and_one_seen_inside_it_is_not(app, fake):
    with app() as c:
        cid = customer(c)
        pay(c, fake, cid, status="confirming")
        assert status(c)["invoice"]["detected_after_expiry"] is False
        with session_scope() as s:  # the transfer was first seen 30 minutes after the invoice was made
            bp = s.query(BillingPayment).one()
            for e in s.query(PaymentEvent).all():
                e.created_at = bp.issued_at + timedelta(minutes=30)
        inv = status(c)["invoice"]
        assert inv["detected_after_expiry"] is True and inv["stage"] == "confirming" and inv["reference"] == REF


def test_an_invoice_that_never_got_a_link_is_skipped(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid, "mbacrypto_old", status="pending")
        issue(cid, "mbacrypto_dead", status="failed")  # created last: the provider refused it
        assert status(c)["invoice"]["reference"] == "mbacrypto_old" or status(c)["invoice"]["reference"] == "mbacrypto_dead"
        with session_scope() as s:
            b = s.query(BillingPayment).filter_by(reference="mbacrypto_dead").one()
            b.issued_at = b.issued_at + timedelta(minutes=5)
        assert status(c)["invoice"]["reference"] == "mbacrypto_old"






















def test_a_paid_invoice_with_no_events_still_reads_as_confirmed(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid, status="paid")
        assert status(c)["invoice"]["stage"] == "confirmed"

