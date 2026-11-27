"""The customer's USDT panel, server side (docs/safety/crypto_payment_panel.md): GET /api/billing/crypto/status (our own database only) and POST /api/billing/crypto/check ("I have sent the payment").

PROVES (fake NOWPayments, scratch SQLite): the status route reads only our database and says plain stages; the check route is gated by the switches and by NOWPAYMENTS_IPN_MODE (off: 404), answers only for the logged-in
customer's own invoice, is limited (one call per 15 seconds per customer, 20 per invoice), writes NOTHING in observe mode, and in enforce mode passes the record through the same apply rule as the IPN (the same access
result); an underpaid payment says how much is short; a payment seen after the window is flagged.
DOES NOT PROVE: the real shape of NOWPayments' answers (UNVERIFIED, as in test_nowpayments.py) or anything in a browser."""
from __future__ import annotations

from datetime import timedelta

import pytest
from test_nowpayments import U, app, bp_state, count, customer, entitled, fake, ipn, issue, pay, record, sub_row, tg, trigger  # noqa: F401
from test_security_hardening import make_app  # noqa: F401

from mba_bot.api.ratelimit import SlidingWindowLimiter
from mba_bot.api.routers import billing as billing_router
from mba_bot.billing import nowpayments as npay
from mba_bot.db import session_scope
from mba_bot.models import BillingPayment, PaymentEvent, Subscription

REF = "mbacrypto_ref1"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def fresh_limits():
    clock = Clock()
    billing_router._check_limiter = SlidingWindowLimiter(clock=clock)
    npay.reset_seen_for_tests()
    yield clock
    npay.reset_seen_for_tests()


def status(c):
    r = c.get("/api/billing/crypto/status")
    assert r.status_code == 200, r.text
    return r.json()


def check(c, **body):
    return c.post("/api/billing/crypto/check", json=body)


def writes():
    """Everything a check must not change in observe mode."""
    with session_scope() as s:
        return (s.query(PaymentEvent).count(), s.query(Subscription).count(), [(b.status, b.provider_payment_id, str(b.usd_value)) for b in s.query(BillingPayment).all()])


# ====================================================================== gating
def test_both_routes_answer_404_while_billing_or_nowpayments_is_off_and_401_without_a_login(app, fake):
    with app() as c:
        assert c.get("/api/billing/crypto/status").status_code == 401 and check(c).status_code == 401
    with app(NOWPAYMENTS_ENABLED="0") as c:
        customer(c, "e1@example.com")
        assert c.get("/api/billing/crypto/status").status_code == 404 and check(c).status_code == 404
    with app(SUBSCRIPTION_BILLING_ENABLED="0") as c:
        customer(c, "e2@example.com")
        assert c.get("/api/billing/crypto/status").status_code == 404 and check(c).status_code == 404


def test_the_check_answers_404_in_off_mode_like_the_other_routes_and_makes_no_call(app, fake):
    with app(NOWPAYMENTS_IPN_MODE="off") as c:
        cid = customer(c)
        issue(cid)
        r = check(c)
        assert r.status_code == 404 and fake.calls == []
        s = status(c)  # the status route still answers, and says the check is not available
        assert s["mode"] == "off" and s["check_available"] is False


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


# ====================================================================== the check route
def test_enforce_check_passes_a_finished_payment_through_the_same_rule_as_the_ipn(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        with session_scope() as s:
            s.query(BillingPayment).one().provider_payment_id = "5077125051"
        fake.payments["5077125051"] = record(REF, "finished")
        r = check(c)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stage"] == "confirmed" and body["recorded"] is True and body["mode"] == "enforce"
        by_check = (sub_row(cid), bp_state(), entitled(cid))
    with app() as c:  # the IPN path, for another customer: the same result
        cid2 = customer(c, "ipn@example.com")
        pay(c, fake, cid2, order="mbacrypto_ref9", pid=9009)
        by_ipn = (sub_row(cid2), bp_state("mbacrypto_ref9"), entitled(cid2))
    assert by_check[2] is True and by_ipn[2] is True
    assert by_check[0][0] == by_ipn[0][0] == "active" and by_check[0][3] == by_ipn[0][3] == "nowpayments"
    assert by_check[1][0] == by_ipn[1][0] == "paid" and by_check[1][2] == by_ipn[1][2]


def test_observe_check_writes_nothing_and_only_returns_the_status(app, fake):
    with app(NOWPAYMENTS_IPN_MODE="observe") as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = record(REF, "confirming")
        assert ipn(c, trigger(REF, "confirming")).status_code == 200  # an observed IPN: the payment id is remembered in memory, nothing is written
        fake.payments["5077125051"] = record(REF, "finished")
        before = writes()
        r = check(c)
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "observe" and body["recorded"] is False and body["stage"] == "confirmed" and "support" in body["text"]
        assert writes() == before and sub_row(cid) is None and not entitled(cid)
        assert status(c)["invoice"]["stage"] == "waiting"  # our database still knows nothing


def test_a_check_with_no_payment_seen_yet_asks_nobody(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        body = check(c).json()
        assert body["stage"] == "no_payment_seen" and body["recorded"] is False and fake.calls == []


def test_an_underpaid_check_says_how_much_is_short_in_both_modes(app, fake):
    for mode in ("enforce", "observe"):
        with app(NOWPAYMENTS_IPN_MODE=mode) as c:
            cid = customer(c, f"{mode}@example.com")
            ref = f"mbacrypto_{mode}"
            issue(cid, ref)
            with session_scope() as s:
                s.query(BillingPayment).filter_by(reference=ref).one().provider_payment_id = "8100"
            fake.payments["8100"] = record(ref, "partially_paid", pid=8100, pay_amount=10.0, actually_paid=7.5)
            body = check(c, reference=ref).json()
            assert body["stage"] == "underpaid" and body["short_usd"] == "2.50", (mode, body)


def test_another_customers_reference_gets_a_plain_404_and_no_call_and_burns_no_quota(app, fake):
    with app() as c:
        a = customer(c, "a@example.com")
        issue(a, "mbacrypto_a")
        with session_scope() as s:
            s.query(BillingPayment).one().provider_payment_id = "1"
        fake.payments["1"] = record("mbacrypto_a", "finished", pid=1)
        b = customer(c, "b@example.com")  # the same browser is now customer B
        issue(b, "mbacrypto_b")
        r = check(c, reference="mbacrypto_a")
        assert r.status_code == 404 and fake.calls == []
        assert check(c, reference="mbacrypto_nope").status_code == 404
        assert count(Subscription) == 0 and not entitled(a)
        assert check(c, reference="x" * 200).status_code == 404
        # B's own check is not blocked by A's attempts
        assert check(c, reference="mbacrypto_b").status_code == 200


def test_a_payment_that_belongs_to_another_customers_invoice_is_ignored(app, fake):
    with app() as c:
        a = customer(c, "a@example.com")
        issue(a, "mbacrypto_a")
        b = customer(c, "b@example.com")
        issue(b, "mbacrypto_b")
        with session_scope() as s:
            s.query(BillingPayment).filter_by(reference="mbacrypto_b").one().provider_payment_id = "55"  # B's row points at a payment that is really A's
        fake.payments["55"] = record("mbacrypto_a", "finished", pid=55)
        before = writes()
        body = check(c, reference="mbacrypto_b").json()
        assert body["stage"] == "unclear" and body["recorded"] is False
        assert writes() == before and not entitled(a) and not entitled(b)


def test_one_call_per_customer_per_fifteen_seconds_and_twenty_per_invoice(app, fake, fresh_limits):
    clock = fresh_limits
    with app() as c:
        cid = customer(c)
        issue(cid)
        assert check(c).status_code == 200
        r = check(c)
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        clock.t += 14.9
        assert check(c).status_code == 429
        clock.t += 0.2  # 15.1 s after the first
        assert check(c).status_code == 200
        for _ in range(18):  # calls 3..20, each 16 s apart
            clock.t += 16
            assert check(c).status_code == 200
        clock.t += 16
        r = check(c)
        assert r.status_code == 429 and "support" in r.json()["detail"]  # the 21st for this invoice


def test_a_rate_limited_call_asks_nobody(app, fake, fresh_limits):
    with app() as c:
        cid = customer(c)
        issue(cid)
        with session_scope() as s:
            s.query(BillingPayment).one().provider_payment_id = "5077125051"
        fake.payments["5077125051"] = record(REF, "waiting")
        assert check(c).status_code == 200
        n = len(fake.calls)
        assert check(c).status_code == 429 and len(fake.calls) == n


def test_the_provider_being_down_gives_503_and_writes_nothing(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        with session_scope() as s:
            s.query(BillingPayment).one().provider_payment_id = "5077125051"
        fake.down = True
        before = writes()
        assert check(c).status_code == 503 and writes() == before


def test_a_pending_check_in_enforce_only_notes_that_the_transfer_was_seen(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        with session_scope() as s:
            s.query(BillingPayment).one().provider_payment_id = "5077125051"
        fake.payments["5077125051"] = record(REF, "confirming")
        body = check(c).json()
        assert body["stage"] == "confirming" and body["recorded"] is True
        assert sub_row(cid) is None and not entitled(cid) and bp_state()[0] == "pending"
        assert status(c)["invoice"]["stage"] == "confirming"


def test_a_paid_invoice_with_no_events_still_reads_as_confirmed(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid, status="paid")
        assert status(c)["invoice"]["stage"] == "confirmed"


def test_the_check_finds_the_payment_from_the_database_after_a_restart_in_enforce_mode(app, fake):
    """The IPN's 'seen' note stored the payment id on the invoice, so the check works even when the process has forgotten everything (a restart)."""
    with app() as c:
        cid = customer(c)
        pay(c, fake, cid, status="confirming")
        assert bp_state()[1] == "5077125051"
        npay.reset_seen_for_tests()
        fake.payments["5077125051"] = record(REF, "finished")
        body = check(c).json()
        assert body["stage"] == "confirmed" and body["recorded"] is True and entitled(cid)
