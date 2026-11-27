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
        assert s["invoice"]["stage"] == "waiting" and s["invoice"]["reference"] == REF and s["invoice"]["price_usd"] == "10.00"
        assert fake.calls == [] and fake.posts == []


def test_the_status_carries_no_time_limit_of_ours_only_when_the_invoice_was_made_and_the_server_time(app, fake):
    from datetime import datetime

    with app() as c:
        cid = customer(c)
        issue(cid)
        body = status(c)
        assert "window_minutes" not in body and "window_ends_at" not in body["invoice"]
        assert abs((datetime.fromisoformat(body["server_time"]) - datetime.now(U)).total_seconds()) < 30 and body["invoice"]["issued_at"]


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


def test_a_transfer_is_flagged_as_late_only_after_the_providers_own_expired_status_never_by_our_age(app, fake):
    with app() as c:
        cid = customer(c)
        pay(c, fake, cid, status="confirming")
        with session_scope() as s:  # the invoice is a month old: our own age means nothing
            bp = s.query(BillingPayment).one()
            bp.issued_at = bp.issued_at - timedelta(days=30)
            for e in s.query(PaymentEvent).all():
                e.created_at = e.created_at - timedelta(days=30)
        assert status(c)["invoice"]["detected_after_expiry"] is False and status(c)["invoice"]["stage"] == "confirming"
    with app() as c:
        cid = customer(c, "late@example.com")
        issue(cid, "mbacrypto_late")
        fake.payments["71"] = record("mbacrypto_late", "expired", pid=71)
        ipn(c, trigger("mbacrypto_late", "expired", 71))  # the provider says the payment expired
        assert status(c)["invoice"]["stage"] == "expired" and status(c)["invoice"]["detected_after_expiry"] is False
        fake.payments["72"] = record("mbacrypto_late", "confirming", pid=72)
        ipn(c, trigger("mbacrypto_late", "confirming", 72))  # then a transfer is seen
        inv = status(c)["invoice"]
        assert inv["detected_after_expiry"] is True and inv["stage"] == "confirming" and inv["reference"] == "mbacrypto_late"


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



@pytest.mark.parametrize("settled", ["paid", "refunded"])
def test_a_late_pending_notice_for_an_invoice_that_is_already_settled_writes_nothing(app, fake, settled):
    """The guard in the apply rule: a waiting or confirming notice that arrives after the invoice was paid or refunded must not write an event or replace the payment id."""
    with app() as c:
        cid = customer(c)
        issue(cid, status=settled)
        with session_scope() as s:
            s.query(BillingPayment).one().provider_payment_id = "4242"
        before_events, before = count(PaymentEvent), bp_state()
        for st in ("waiting", "confirming"):
            fake.payments["5077125051"] = record(REF, st)
            assert ipn(c, trigger(REF, st)).json() == {"ok": True, "pending": st}
        assert count(PaymentEvent) == before_events == 0 and bp_state() == before and bp_state()[1] == "4242"


@pytest.mark.parametrize("state", ["underpaid", "expired", "failed"])
def test_a_new_payment_on_an_invoice_that_was_underpaid_expired_or_failed_is_still_noted(app, fake, state):
    """These states are not final: the customer can pay again on the same invoice, and the page must follow that payment."""
    with app() as c:
        cid = customer(c)
        issue(cid, status=state)
        fake.payments["5077125051"] = record(REF, "confirming")
        ipn(c, trigger(REF, "confirming"))
        assert count(PaymentEvent) == 1 and bp_state()[1] == "5077125051" and bp_state()[0] == state and not entitled(cid)
        assert status(c)["invoice"]["stage"] == "confirming"


def test_a_pending_notice_for_a_pending_invoice_still_notes_the_payment(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = record(REF, "confirming")
        ipn(c, trigger(REF, "confirming"))
        assert count(PaymentEvent) == 1 and bp_state()[1] == "5077125051"


def test_the_coin_comes_from_the_setting_and_an_unknown_code_is_shown_as_the_code(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        coin = status(c)["invoice"]["pay_coin"]
        assert coin["code"] == "usdtbsc" and coin["known"] is True and coin["asset"] == "USDT" and coin["network"] == "the BSC network (BEP20)" and coin["network_short"] == "BSC (BEP20)"
    with app(NOWPAYMENTS_PAY_CURRENCY="ltc") as c:
        cid = customer(c, "ltc@example.com")
        issue(cid, "mbacrypto_ltc")
        coin = status(c)["invoice"]["pay_coin"]
        assert coin == {"code": "ltc", "known": False, "asset": "LTC", "network": None, "network_short": None, "examples": None, "label": "LTC"}
