"""A payment in a different coin or network (docs/safety/crypto_payment_panel.md, 'Wrong coin or network').

The live test showed it: the invoice asked for USDT on BSC, the customer sent another coin, NOWPayments converted it and made a CHILD payment linked to the invoice's own (parent) payment. The child cannot be read with
our API key (HTTP 404), so a verified notification for it used to be logged and dropped.

PROVES (fake NOWPayments, scratch SQLite): such a notification, found only by what the verified notification itself carries (order id, parent payment id, coin), is never credited in any mode, never uses an amount
from the notification, marks the invoice (a payment event in enforce mode; the process's memory only in observe mode, which writes nothing), alerts the owner once per invoice with a masked order tail, and shows on
the page as its own stage; a notification that names no invoice, or looks like an ordinary unreadable payment, is ignored as before; the parent's later notices do not hide the stage, and a later correct payment is
still applied. The invoice's coin, network and exact amount reach the page.
DOES NOT PROVE: what NOWPayments really sends for a child (the notification fields used are the ones the live test showed; UNVERIFIED beyond that)."""
from __future__ import annotations

import pytest
from test_nowpayments import app, bp_state, count, customer, entitled, fake, ipn, issue, record, sub_row, tg, trigger  # noqa: F401
from test_security_hardening import make_app  # noqa: F401

from mba_bot.billing import nowpayments as npay, subscriptions as subs
from mba_bot.db import session_scope
from mba_bot.models import BillingPayment, PaymentEvent

REF = "mbacrypto_ref1"
PARENT, CHILD, CHILD2 = 5077125051, 6000000001, 6000000002


@pytest.fixture(autouse=True)
def clean_memory():
    npay.reset_seen_for_tests()
    yield
    npay.reset_seen_for_tests()


def child(order=REF, *, pid=CHILD, parent=PARENT, pay_currency="bnb", **over):
    """What the verified notification for a converted child carries. It claims a finished payment and amounts: none of that may be believed."""
    body = trigger(order, "finished", pid, pay_currency=pay_currency, pay_amount=0.01775, actually_paid=0.01775, price_amount=10)
    if parent is not None:
        body["parent_payment_id"] = parent
    if order is None:
        body.pop("order_id")
    body.update(over)
    return body


def events():
    with session_scope() as s:
        return sorted(e.outcome for e in s.query(PaymentEvent).all())


def stage(c):
    return c.get("/api/billing/crypto/status").json()["invoice"]["stage"]


def wrong_alerts(tg):
    return [m for m in tg.to_owner if subs.ALERTS["crypto_wrong_asset"] in m]


# ====================================================================== enforce
def test_an_unreadable_child_with_a_parent_id_is_marked_alerted_once_and_never_credited(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        r = ipn(c, child())
        assert r.status_code == 200 and r.json() == {"ok": True, "recorded": "wrong_coin"}
        assert sub_row(cid) is None and not entitled(cid) and bp_state()[0] == "pending" and bp_state()[2] == 10  # no access, the invoice untouched, the price not replaced by the notification's amount
        assert events() == ["wrong_coin"] and stage(c) == "wrong_coin"
        assert fake.calls == [f"/v1/payment/{CHILD}"]  # the payment was asked for once, as before, and could not be read
    alerts = wrong_alerts(tg)
    assert len(alerts) == 1 and "Order ...o_ref1" in alerts[0] and "mbacrypto_ref1" not in alerts[0] and "@" not in alerts[0]


def test_a_second_notification_for_the_same_invoice_changes_nothing_and_alerts_nobody(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        assert ipn(c, child()).json() == {"ok": True, "recorded": "wrong_coin"}
        assert ipn(c, child(pid=CHILD2, payment_status="confirmed")).json() == {"ok": True, "duplicate": True}
        assert ipn(c, child()).json() == {"ok": True, "duplicate": True}
        assert events() == ["wrong_coin"] and not entitled(cid)
    assert len(wrong_alerts(tg)) == 1


def test_the_alert_is_once_per_invoice_not_once_per_customer(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid, "mbacrypto_ref1")
        issue(cid, "mbacrypto_ref2")
        ipn(c, child("mbacrypto_ref1"))
        ipn(c, child("mbacrypto_ref2", pid=CHILD2, parent=PARENT + 1))
        ipn(c, child("mbacrypto_ref2", pid=CHILD2 + 5, parent=PARENT + 1))
    alerts = wrong_alerts(tg)
    assert len(alerts) == 2 and any("...o_ref1" in a for a in alerts) and any("...o_ref2" in a for a in alerts)


def test_a_child_whose_notification_has_no_parent_id_is_found_by_its_order_id(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        r = ipn(c, child(parent=None))
        assert r.json() == {"ok": True, "recorded": "wrong_coin"} and not entitled(cid) and events() == ["wrong_coin"]
    assert len(wrong_alerts(tg)) == 1


def test_an_unreadable_payment_that_names_our_parent_payment_is_wrong_coin_even_when_it_names_no_coin(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        body = child()
        body.pop("pay_currency")
        assert ipn(c, body).json() == {"ok": True, "recorded": "wrong_coin"} and not entitled(cid)


def test_a_child_whose_notification_has_no_order_id_is_found_by_the_parent_payment_id(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments[str(PARENT)] = record(REF, "waiting", pid=PARENT)
        assert ipn(c, trigger(REF, "waiting", PARENT)).json() == {"ok": True, "pending": "waiting"}  # the parent's own notice stores its id on the invoice
        assert bp_state()[1] == str(PARENT)
        r = ipn(c, child(order=None))
        assert r.json() == {"ok": True, "recorded": "wrong_coin"} and not entitled(cid) and stage(c) == "wrong_coin"


def test_a_notification_that_names_no_invoice_of_ours_is_ignored_as_before(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        assert ipn(c, child(order="mbacrypto_nobody", parent=999)).json() == {"ok": True, "ignored": "payment_not_found"}
        assert ipn(c, child(order=None, parent=None)).json() == {"ok": True, "ignored": "payment_not_found"}
        assert events() == [] and not entitled(cid)
    assert wrong_alerts(tg) == []


def test_an_ordinary_unreadable_payment_in_the_right_coin_with_no_parent_is_not_called_wrong_coin(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        assert ipn(c, child(parent=None, pay_currency="usdtbsc")).json() == {"ok": True, "ignored": "payment_not_found"}
        assert events() == [] and stage(c) == "waiting"
    assert wrong_alerts(tg) == []


def test_a_readable_record_in_another_coin_is_the_same_once_per_invoice(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["77"] = record(REF, "finished", pid=77, pay_currency="usdttrc20")
        assert ipn(c, trigger(REF, "finished", 77, pay_currency="usdttrc20")).json() == {"ok": True, "recorded": "wrong_coin"}
        assert ipn(c, trigger(REF, "finished", 77, pay_currency="usdttrc20")).json() == {"ok": True, "duplicate": True}
        assert not entitled(cid) and stage(c) == "wrong_coin"
    assert len(wrong_alerts(tg)) == 1


def test_the_parents_later_waiting_notice_does_not_hide_the_stage_and_a_later_correct_payment_is_still_applied(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        ipn(c, child())
        fake.payments[str(PARENT)] = record(REF, "waiting", pid=PARENT)
        ipn(c, trigger(REF, "waiting", PARENT))
        npay.reset_seen_for_tests()  # the stage must come from the database, not from this process's memory
        assert stage(c) == "wrong_coin"
        fake.payments["8001"] = record(REF, "finished", pid=8001)  # a second payment, in the right coin
        assert ipn(c, trigger(REF, "finished", 8001)).json() == {"ok": True, "applied": True}
        assert entitled(cid) and stage(c) == "confirmed"


def test_a_child_record_can_never_replace_the_parent_payment_id_on_the_invoice(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments[str(PARENT)] = record(REF, "waiting", pid=PARENT)
        ipn(c, trigger(REF, "waiting", PARENT))
        fake.payments[str(CHILD)] = record(REF, "confirming", pid=CHILD, parent_payment_id=PARENT)  # a readable child
        ipn(c, child())
        assert bp_state()[1] == str(PARENT) and not entitled(cid)


# ====================================================================== observe and off
def test_observe_writes_nothing_and_shows_the_stage_from_memory_only(app, fake, tg):
    with app(NOWPAYMENTS_IPN_MODE="observe") as c:
        cid = customer(c)
        issue(cid)
        before = (count(PaymentEvent), bp_state())
        assert ipn(c, child()).json() == {"ok": True, "observed": "wrong_coin"}
        assert (count(PaymentEvent), bp_state()) == before and not entitled(cid)
        assert stage(c) == "wrong_coin"  # the process remembers it: no write was needed
        npay.reset_seen_for_tests()  # a restart forgets it, which is the limit of observe mode
        assert stage(c) == "waiting"
    assert wrong_alerts(tg) == []  # the owner alert is for enforce mode


def test_off_mode_ignores_it_entirely(app, fake, tg):
    with app(NOWPAYMENTS_IPN_MODE="off") as c:
        cid = customer(c)
        issue(cid)
        assert ipn(c, child()).json() == {"ok": True, "ignored": "mode_off"}
        assert events() == [] and not entitled(cid)


@pytest.mark.parametrize("mode", ["enforce", "observe"])
def test_no_access_is_granted_in_any_mode_whatever_the_notification_claims(app, fake, tg, mode):
    with app(NOWPAYMENTS_IPN_MODE=mode) as c:
        cid = customer(c)
        issue(cid)
        for i, over in enumerate([{}, {"payment_status": "confirmed"}, {"actually_paid": 9999, "pay_amount": 1, "price_amount": 10}, {"outcome_amount": 10}]):
            ipn(c, child(pid=CHILD + i, **over))
        assert sub_row(cid) is None and not entitled(cid) and bp_state()[0] == "pending"


# ====================================================================== the instructions
def test_the_status_carries_the_coin_the_network_and_the_exact_amount_once_the_payment_exists(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        inv = c.get("/api/billing/crypto/status").json()["invoice"]
        assert inv["pay_label"] == "USDT on BSC (BEP20)" and inv["pay_amount"] is None
        fake.payments[str(PARENT)] = record(REF, "waiting", pid=PARENT, pay_amount=9.954, actually_paid=0)
        ipn(c, trigger(REF, "waiting", PARENT))
        inv = c.get("/api/billing/crypto/status").json()["invoice"]
        assert inv["pay_amount"] == "9.954" and inv["pay_label"] == "USDT on BSC (BEP20)"
    with app(NOWPAYMENTS_IPN_MODE="observe") as c:  # observe mode keeps the amount in memory
        cid2 = customer(c, "obs@example.com")
        issue(cid2, "mbacrypto_obs")
        fake.payments["91"] = record("mbacrypto_obs", "waiting", pid=91, pay_amount=9.954, actually_paid=0)
        ipn(c, trigger("mbacrypto_obs", "waiting", 91))
        assert c.get("/api/billing/crypto/status").json()["invoice"]["pay_amount"] == "9.954"


def test_another_configured_coin_is_shown_under_its_own_code(app, fake, tg):
    with app(NOWPAYMENTS_PAY_CURRENCY="usdttrc20") as c:
        cid = customer(c)
        issue(cid)
        assert c.get("/api/billing/crypto/status").json()["invoice"]["pay_label"] == "USDTTRC20"
