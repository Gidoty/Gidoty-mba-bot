"""Stage 1 of designs/03: Paystack subscriptions behind SUBSCRIPTION_BILLING_ENABLED.

PROVES: parsing, control flow, the verification ladder, idempotency and the entitlement outcome, against a FAKE Paystack
(one replaced seam: billing.paystack_sub.get_json). DOES NOT PROVE: what Paystack's real webhook bodies look like (none
were captured; every body here is invented and the handlers must tolerate them being wrong), what a real renewal charge
carries (fee or not), the real timing between charge.success and the subscription record, or how Paystack retries.
No network, no live key, no real exchange."""
from __future__ import annotations

import ast
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from test_security_hardening import SK, client_for, make_app, post_event, sign  # noqa: F401

from mba_bot import crypto_utils, protection_guard
from mba_bot.api.routers import billing
from mba_bot.billing import paystack_sub as ps
from mba_bot.billing import subscriptions as subs
from mba_bot.billing.entitlement import is_entitled
from mba_bot.config import load_settings
from mba_bot.db import session_scope
from mba_bot.models import BillingPayment, Customer, PaymentEvent, Subscription, SubscriptionToken, TradingAccount

PLAN = "PLN_test"
PRICE = 1_500_000
FEES = 22_500
TOKEN = "tok_SECRETsubscriptiontoken123"
SIGNUP = {"password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True}
ENV = {"SUBSCRIPTION_BILLING_ENABLED": "1", "PAYSTACK_PLAN_CODE": PLAN, "PAYSTACK_PLAN_AMOUNT": str(PRICE),
       "TELEGRAM_BOT_TOKEN": "123:fake", "BILLING_ALERT_CHAT_ID": "999"}


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakePaystack:
    """Paystack as the handlers see it. Tests set .txs / .subs / .customers and read .calls."""

    def __init__(self):
        self.txs, self.subs, self.customers, self.calls = {}, {}, {}, []
        self.plan_id, self.down, self.on_call = 77, False, None

    async def get_json(self, path, settings):
        self.calls.append(path)
        if self.on_call:
            self.on_call(path)
        if self.down:
            raise ps.ProviderUnavailable("http 503")
        if path.startswith("/transaction/verify/"):
            tx = self.txs.get(unquote(path.rsplit("/", 1)[1]))
            return None if tx is None else {"status": True, "data": tx}
        if path.startswith("/subscription?"):
            q = parse_qs(urlparse(path).query)
            assert q["plan"] == [str(self.plan_id)], "the list must be filtered by the NUMERIC plan id"
            data = [s for s in self.subs.values() if str(s["customer"]["id"]) == q["customer"][0]]
            return {"status": True, "data": data}
        if path.startswith("/subscription/"):
            s = self.subs.get(path.rsplit("/", 1)[1])
            return None if s is None else {"status": True, "data": s}
        if path.startswith("/plan/"):
            return {"status": True, "data": {"id": self.plan_id, "plan_code": PLAN}}
        if path.startswith("/customer/"):
            c = self.customers.get(unquote(path.rsplit("/", 1)[1]).lower())
            return None if c is None else {"status": True, "data": c}
        raise AssertionError(path)

    # builders
    def tx(self, ref, cid, email, *, amount=PRICE + FEES, requested=PRICE, fees=FEES, currency="NGN", status="success", meta=True, plan=PLAN, **over):
        t = {"id": 4242, "status": status, "reference": ref, "amount": amount, "requested_amount": requested, "fees": fees,
             "currency": currency, "paid_at": iso(datetime.now(timezone.utc)), "customer": {"id": 501, "email": email},
             "authorization": {"reusable": True, "authorization_code": "AUTH_x"}, "metadata": {"customer_id": cid} if meta else {},
             "plan": {"plan_code": plan} if plan else {}}
        t.update(over)
        self.txs[ref] = t
        self.customers[email.lower()] = {"id": 501, "email": email}
        return t

    def sub(self, code, email, end, *, status="active", plan=PLAN, reusable=True, **over):
        s = {"subscription_code": code, "status": status, "amount": PRICE, "next_payment_date": iso(end) if end else None,
             "email_token": TOKEN, "createdAt": iso(datetime.now(timezone.utc)), "plan": {"plan_code": plan, "amount": PRICE},
             "customer": {"id": 501, "email": email}, "authorization": {"reusable": reusable}}
        s.update(over)
        self.subs[code] = s
        return s


@pytest.fixture()
def fake(monkeypatch):
    f = FakePaystack()
    monkeypatch.setattr(ps, "get_json", f.get_json)
    return f


@pytest.fixture()
def sent(monkeypatch):
    protection_guard.reset_for_tests()
    out = []

    class Notifier:
        def __init__(self, token):
            pass

        async def send(self, target, message):
            out.append((target, message))

    monkeypatch.setattr(subs, "TelegramNotifier", Notifier)
    return out


@pytest.fixture()
def app_on(make_app, sent):  # `sent` is always active so no test can reach the real Telegram
    return lambda **extra: client_for(make_app(**{**ENV, **extra}))


def signed_up(c, email="payer@example.com") -> str:
    r = c.post("/api/auth/signup", json={"email": email, **SIGNUP})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def issue(cid, ref="mbasub_ref1"):
    with session_scope() as s:
        s.add(BillingPayment(customer_id=cid, provider="paystack", reference=ref, amount_minor=PRICE, currency="NGN", status="pending"))


def charge_event(ref, email=None, **extra):
    d = {"reference": ref, "status": "success", **extra}
    if email:
        d["customer"] = {"email": email}
    return {"event": "charge.success", "data": d}


def row(cid):
    with session_scope() as s:
        r = s.query(Subscription).filter_by(customer_id=cid).first()
        return None if r is None else (r.status, r.current_period_end, r.provider_subscription_ref, r.current_period_start)


def entitled(cid, now=None):
    with session_scope() as s:
        return is_entitled(s.get(Customer, cid), settings=load_settings(), now=now)


def count(model):
    with session_scope() as s:
        return s.query(model).count()


def end_in(days, hours=3):  # a deliberately irregular instant: nothing here may be recomputed by our code
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=days, hours=hours)


def first_payment(c, fake, email="payer@example.com", ref="mbasub_ref1", end=None):
    cid = signed_up(c, email)
    issue(cid, ref)
    fake.tx(ref, cid, email)
    fake.sub("SUB_1", email, end or end_in(30))
    return cid


# ====================================================================== switch OFF: nothing changes
def test_with_the_switch_off_a_complete_valid_payment_is_not_acted_on(make_app, fake, sent):
    with client_for(make_app()) as c:
        cid = first_payment(c, fake)
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 200
        assert post_event(c, {"event": "subscription.create", "data": {"subscription_code": "SUB_1", "customer": {"email": "payer@example.com"}}}).status_code == 200
        assert row(cid) is None and fake.calls == [] and count(PaymentEvent) == 0
        with session_scope() as s:
            assert s.query(BillingPayment).one().status == "pending"


def test_a_concurrent_delivery_that_already_recorded_the_payment_wins(app_on, fake):
    """The payment_events unique key is the last line of replay protection: if another delivery recorded it first, this one grants nothing."""
    with app_on() as c:
        cid = first_payment(c, fake)
        with session_scope() as s:
            s.add(PaymentEvent(provider="paystack", event_key="sub-charge:mbasub_ref1", reference="mbasub_ref1", customer_id=cid, amount=PRICE, currency="NGN", outcome="applied"))
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 200
        assert row(cid) is None and not entitled(cid)
        with session_scope() as s:
            assert s.query(BillingPayment).one().status == "pending"


@pytest.mark.parametrize("final", ["completed", "cancelled"])
def test_a_payment_against_a_finished_subscription_grants_nothing(app_on, fake, final):
    with app_on() as c:
        cid = first_payment(c, fake)
        fake.subs["SUB_1"]["status"] = final
        assert post_event(c, charge_event("mbasub_ref1")).status_code in (200, 503)
        assert row(cid) is None and not entitled(cid)


def test_with_the_switch_off_the_new_route_does_not_exist_and_events_are_ignored(make_app, fake, sent):
    with client_for(make_app()) as c:  # no SUBSCRIPTION_BILLING_ENABLED
        cid = signed_up(c)
        assert c.post("/api/billing/subscription/initialize").status_code == 404
        for ev in ({"event": "subscription.create", "data": {"customer": {"email": "payer@example.com"}}},
                   {"event": "charge.success", "data": {"reference": "x", "plan": {"plan_code": PLAN}}},
                   {"event": "invoice.payment_failed", "data": {}}):
            assert post_event(c, ev).status_code == 200
        assert fake.calls == [] and sent == []
        assert count(Subscription) == 0 and count(BillingPayment) == 0 and count(SubscriptionToken) == 0
        assert not entitled(cid)


def test_legacy_activation_still_works_with_the_switch_on_and_legacy_customers_stay_lifetime(app_on, fake, sent):
    with app_on() as c:
        cid = signed_up(c)
        ev = {"event": "charge.success", "data": {"status": "success", "reference": "old_ref", "metadata": {"customer_id": cid}}}
        assert post_event(c, ev).status_code == 200  # the existing handler ran: it is not a subscription charge
        with session_scope() as s:
            assert s.get(Customer, cid).has_paid is True
        assert entitled(cid) and count(Subscription) == 0
        assert fake.calls == []


# ====================================================================== checkout
class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        if self._b is None:
            raise ValueError
        return self._b


@pytest.mark.parametrize("code", [200, 201])
def test_initialize_sends_the_plan_and_no_amount_and_records_the_price_asked(app_on, monkeypatch, code):
    seen = {}

    def post(url, headers=None, json=None, timeout=None):
        seen.update(json=json, url=url)
        return _Resp(code, {"status": True, "data": {"authorization_url": "https://checkout.example/x", "reference": json["reference"]}})

    monkeypatch.setattr(billing.httpx, "post", post)
    with app_on() as c:
        cid = signed_up(c)
        r = c.post("/api/billing/subscription/initialize")
        assert r.status_code == 200 and r.json()["authorization_url"] == "https://checkout.example/x"
        sent_body = seen["json"]
        assert sent_body["plan"] == PLAN and "amount" not in sent_body and "currency" not in sent_body
        assert sent_body["metadata"]["customer_id"] == cid
        with session_scope() as s:
            bp = s.query(BillingPayment).one()
            assert (bp.reference, bp.amount_minor, bp.currency, bp.status, bp.customer_id) == (sent_body["reference"], PRICE, "NGN", "pending", cid)


@pytest.mark.parametrize("resp", [_Resp(400, {"status": False}), _Resp(200, None), _Resp(200, {"status": False}), _Resp(500, {"status": True, "data": {"authorization_url": "u"}})])
def test_initialize_failures_hand_out_no_link(app_on, monkeypatch, resp):
    monkeypatch.setattr(billing.httpx, "post", lambda *a, **k: resp)
    with app_on() as c:
        signed_up(c)
        assert c.post("/api/billing/subscription/initialize").status_code == 502


def test_initialize_refuses_lifetime_and_active_customers_and_a_missing_plan(app_on, fake, monkeypatch):
    monkeypatch.setattr(billing.httpx, "post", lambda *a, **k: pytest.fail("must not reach Paystack"))
    with app_on() as c:
        cid = signed_up(c)
        with session_scope() as s:
            s.get(Customer, cid).has_paid = True
        assert c.post("/api/billing/subscription/initialize").status_code == 409
        with session_scope() as s:
            cu = s.get(Customer, cid)
            cu.has_paid = False
            s.add(Subscription(customer_id=cid, status="active", current_period_end=end_in(10), provider="paystack"))
        assert c.post("/api/billing/subscription/initialize").status_code == 409
    with app_on(PAYSTACK_PLAN_CODE="") as c2:
        signed_up(c2, "b@example.com")
        assert c2.post("/api/billing/subscription/initialize").status_code in (401, 503, 409)


# ====================================================================== first payment
def test_first_payment_creates_the_subscription_from_paystacks_own_date(app_on, fake, sent):
    end = end_in(30)
    with app_on() as c:
        cid = first_payment(c, fake, end=end)
        assert not entitled(cid)
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 200
        st, period_end, ref, start = row(cid)
        assert (st, ref) == ("active", "SUB_1")
        assert period_end.replace(tzinfo=timezone.utc) == end  # exactly Paystack's next_payment_date
        assert entitled(cid)
        with session_scope() as s:
            bp = s.query(BillingPayment).one()
            assert bp.status == "paid" and bp.period_end is not None and bp.provider_payment_id == "4242"
            assert s.query(PaymentEvent).one().event_key == "sub-charge:mbasub_ref1"
    assert sent == []


def test_a_replayed_event_does_nothing_more(app_on, fake):
    with app_on() as c:
        cid = first_payment(c, fake)
        assert post_event(c, charge_event("mbasub_ref1")).json() == {"ok": True}
        for _ in range(3):
            r = post_event(c, charge_event("mbasub_ref1"))
            assert r.status_code == 200 and r.json() == {"ok": True, "duplicate": True}
        assert count(Subscription) == 1 and count(PaymentEvent) == 1 and count(BillingPayment) == 1
        before = row(cid)
        assert post_event(c, {"event": "subscription.create", "data": {"subscription_code": "SUB_1", "customer": {"email": "payer@example.com"}}}).status_code == 200
        assert row(cid) == before


def test_subscription_create_may_arrive_before_the_charge_event(app_on, fake):
    with app_on() as c:
        cid = first_payment(c, fake)
        assert post_event(c, {"event": "subscription.create", "data": {"subscription_code": "SUB_1", "customer": {"email": "payer@example.com"}}}).status_code == 200
        assert row(cid)[0] == "active" and entitled(cid)
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 200  # now a duplicate
        assert count(PaymentEvent) == 1


def test_a_missing_subscription_record_is_retried_not_guessed(app_on, fake):
    with app_on() as c:
        cid = signed_up(c)
        issue(cid)
        fake.tx("mbasub_ref1", cid, "payer@example.com")  # paid, but Paystack has not created the subscription yet
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 503
        assert row(cid) is None and not entitled(cid) and count(PaymentEvent) == 0
        fake.sub("SUB_1", "payer@example.com", end_in(30))
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 200 and entitled(cid)


def test_paystack_unreachable_is_a_503_and_changes_nothing(app_on, fake):
    with app_on() as c:
        cid = first_payment(c, fake)
        fake.down = True
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 503
        assert row(cid) is None and count(PaymentEvent) == 0 and not entitled(cid)


def test_a_bad_signature_is_refused_and_nothing_is_read(app_on, fake):
    with app_on() as c:
        first_payment(c, fake)
        assert post_event(c, charge_event("mbasub_ref1"), signature="0" * 128).status_code == 400
        assert fake.calls == [] and count(Subscription) == 0


def test_the_signature_is_over_the_raw_bytes(app_on, fake):
    import json as _json
    body = b'{"event":"charge.success",  "data":{"reference":"mbasub_ref1"}}'
    with app_on() as c:
        first_payment(c, fake)
        resub = _json.dumps(_json.loads(body)).encode()
        assert resub != body
        r = c.post("/api/billing/webhook", content=resub, headers={"x-paystack-signature": sign(body)})
        assert r.status_code == 400
        r = c.post("/api/billing/webhook", content=body, headers={"x-paystack-signature": sign(body)})
        assert r.status_code == 200 and count(Subscription) == 1


# ---------------------------------------------------------------------- the ladder
@pytest.mark.parametrize("name,over,alert", [
    ("overpaid_unexplained", dict(amount=PRICE + 50_000, requested=PRICE, fees=FEES), "amount_problem"),
    ("underpaid", dict(amount=PRICE - 1, requested=PRICE - 1, fees=0), "amount_underpaid"),
    ("fee_missing", dict(amount=PRICE + FEES, requested=None, fees=None), "amount_problem"),
    ("wrong_currency", dict(currency="USD"), "payment_rejected"),
    ("other_customer_metadata", dict(metadata={"customer_id": "someone-else"}), "payment_rejected"),
    ("other_email", dict(customer={"id": 9, "email": "thief@example.com"}), "payment_rejected"),
    ("no_customer_information_at_all", dict(metadata={}, customer={}), "payment_rejected"),
    ("other_plan", dict(plan={"plan_code": "PLN_other"}), "payment_rejected"),
    ("different_reference", dict(reference="mbasub_other"), "payment_rejected"),
])
def test_every_failed_check_grants_nothing_and_alerts(app_on, fake, sent, name, over, alert):
    with app_on() as c:
        cid = first_payment(c, fake)
        fake.txs["mbasub_ref1"].update(over)
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 200
        assert row(cid) is None and not entitled(cid)
        with session_scope() as s:
            assert s.query(BillingPayment).one().status in ("failed", "underpaid")
    assert any(alert in m or ALERT_TEXT[alert] in m for _, m in sent), sent


ALERT_TEXT = subs.ALERTS


def test_a_payment_paystack_does_not_call_successful_stays_pending(app_on, fake):
    with app_on() as c:
        cid = first_payment(c, fake)
        fake.txs["mbasub_ref1"]["status"] = "abandoned"
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 200
        assert row(cid) is None and not entitled(cid)
        with session_scope() as s:
            assert s.query(BillingPayment).one().status == "pending"


def test_an_unknown_reference_for_an_unknown_customer_grants_nothing(app_on, fake, sent):
    with app_on() as c:
        signed_up(c)
        fake.tx("stranger_ref", "x", "stranger@example.com")
        assert post_event(c, charge_event("stranger_ref", "stranger@example.com", plan={"plan_code": PLAN})).status_code == 200
        assert count(Subscription) == 0 and count(BillingPayment) == 0
    assert sent


def test_a_card_that_cannot_be_reused_still_gets_the_paid_period_but_alerts(app_on, fake, sent):
    with app_on() as c:
        cid = first_payment(c, fake)
        fake.subs["SUB_1"]["authorization"]["reusable"] = False
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 200 and entitled(cid)
    assert any(ALERT_TEXT["card_not_reusable"] in m for _, m in sent)


@pytest.mark.parametrize("bad", ["far", "none", "garbage"])
def test_a_far_future_or_missing_date_is_not_applied(app_on, fake, sent, bad):
    with app_on() as c:
        cid = first_payment(c, fake)
        fake.subs["SUB_1"]["next_payment_date"] = {"far": iso(end_in(400)), "none": None, "garbage": "not a date"}[bad]
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 503
        assert row(cid) is None and not entitled(cid) and count(PaymentEvent) == 0


def test_the_first_payment_activates_the_account_like_the_existing_flow(app_on, fake):
    with app_on() as c:
        cid = first_payment(c, fake)
        with session_scope() as s:
            s.add(TradingAccount(customer_id=cid, pair="BTC/USDT", market_type="spot", notification_channel="telegram",
                                 trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7), is_active=False))
        post_event(c, charge_event("mbasub_ref1"))
        with session_scope() as s:
            assert s.get(Customer, cid).account.is_active is True
        # a renewal does not touch the flag
        with session_scope() as s:
            s.get(Customer, cid).account.is_active = False
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(40))
        post_event(c, renewal_event())
        with session_scope() as s:
            assert s.get(Customer, cid).account.is_active is False


def test_no_database_write_lock_is_held_while_waiting_for_paystack(app_on, fake):
    """SQLite allows one writer. Every Paystack read must happen with no write pending (db.session_scope explains why)."""
    import sqlite3
    path = load_settings().database_url.replace("sqlite:///", "")
    seen = []

    def probe(_path):
        conn = sqlite3.connect(path, timeout=0.2)
        try:
            conn.execute("BEGIN IMMEDIATE")  # fails with "database is locked" if another connection holds a write lock
            seen.append("free")
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    with app_on() as c:
        cid = first_payment(c, fake)
        fake.on_call = probe
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 200
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(40))
        assert post_event(c, renewal_event()).status_code == 200
    assert len(seen) >= 6 and set(seen) == {"free"}


# ====================================================================== token
def test_the_token_is_stored_encrypted_and_never_logged_or_returned(app_on, fake, caplog):
    caplog.set_level(logging.DEBUG)
    with app_on() as c:
        cid = first_payment(c, fake)
        r = post_event(c, charge_event("mbasub_ref1"))
        assert TOKEN not in r.text
        me = c.get("/api/auth/me")
        assert TOKEN not in me.text
        with session_scope() as s:
            enc = s.query(SubscriptionToken).one().token_enc
        assert TOKEN not in enc
        assert crypto_utils.decrypt(enc, key=load_settings().encryption_key) == TOKEN
    assert TOKEN not in caplog.text and "payer@example.com" not in caplog.text


# ====================================================================== renewal
def activate(c, fake, end):
    cid = first_payment(c, fake, end=end)
    assert post_event(c, charge_event("mbasub_ref1")).status_code == 200
    return cid


def renewal_event(ref="RENEW1", email="payer@example.com"):
    return charge_event(ref, email, plan={"plan_code": PLAN})


def test_a_renewal_extends_to_paystacks_new_date_and_never_from_our_own_arithmetic(app_on, fake):
    old = end_in(1)
    with app_on() as c:
        cid = activate(c, fake, old)
        new = end_in(29, hours=5)
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)
        fake.subs["SUB_1"]["next_payment_date"] = iso(new)
        assert post_event(c, renewal_event()).status_code == 200
        st, end, _, _ = row(cid)
        assert end.replace(tzinfo=timezone.utc) == new and st == "active"
        assert post_event(c, renewal_event()).status_code == 200  # replay
        assert row(cid)[1] == end and count(BillingPayment) == 2 and count(PaymentEvent) == 2


def test_a_renewal_whose_period_has_not_advanced_is_retried_then_succeeds(app_on, fake):
    old = end_in(1)
    with app_on() as c:
        cid = activate(c, fake, old)
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)
        assert post_event(c, renewal_event()).status_code == 503  # Paystack's date is still the old one
        assert row(cid)[1].replace(tzinfo=timezone.utc) == old and count(BillingPayment) == 1
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30))
        assert post_event(c, renewal_event()).status_code == 200
        assert row(cid)[1].replace(tzinfo=timezone.utc) > old


@pytest.mark.parametrize("over", [dict(amount=PRICE + 999, requested=PRICE, fees=FEES), dict(amount=PRICE - 100, requested=PRICE - 100, fees=0),
                                  dict(currency="USD")])
def test_renewal_amounts_are_checked_as_strictly_as_the_first_payment(app_on, fake, sent, over):
    old = end_in(1)
    with app_on() as c:
        cid = activate(c, fake, old)
        fake.tx("RENEW1", cid, "payer@example.com", meta=False).update(over)
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30))
        assert post_event(c, renewal_event()).status_code == 200
        assert row(cid)[1].replace(tzinfo=timezone.utc) == old  # not extended
        with session_scope() as s:
            assert s.query(BillingPayment).filter_by(reference="RENEW1").one().status in ("failed", "underpaid")
    assert sent


def test_a_renewal_without_the_fee_is_also_accepted(app_on, fake):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        fake.tx("RENEW1", cid, "payer@example.com", meta=False, amount=PRICE, requested=PRICE, fees=0)
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30))
        assert post_event(c, renewal_event()).status_code == 200 and count(PaymentEvent) == 2


# ====================================================================== failed renewal, cancel, refund
def test_a_failed_renewal_changes_nothing_alerts_and_grace_starts_by_itself(app_on, fake, sent):
    old = end_in(2)
    with app_on() as c:
        cid = activate(c, fake, old)
        fake.subs["SUB_1"]["status"] = "attention"
        ev = {"event": "invoice.payment_failed", "data": {"subscription": {"subscription_code": "SUB_1"}, "customer": {"email": "payer@example.com"}}}
        assert post_event(c, ev).status_code == 200
        st, end, _, _ = row(cid)
        assert st == "active" and end.replace(tzinfo=timezone.utc) == old
        assert any(ALERT_TEXT["renewal_failed"] in m for _, m in sent)
        assert entitled(cid, old - timedelta(seconds=1)) and entitled(cid, old + timedelta(days=4, hours=23))
        assert not entitled(cid, old + timedelta(days=5, seconds=1))  # D2: five days, then expired


def test_cancel_keeps_access_until_the_paid_date_then_ends(app_on, fake, sent):
    old = end_in(20)
    with app_on() as c:
        cid = activate(c, fake, old)
        fake.subs["SUB_1"]["status"] = "non-renewing"  # as the test run showed: status changes, date kept
        for name in ("subscription.not_renew", "subscription.disable"):
            assert post_event(c, {"event": name, "data": {"subscription_code": "SUB_1"}}).status_code == 200
        st, end, _, _ = row(cid)
        assert st == "cancelled" and end.replace(tzinfo=timezone.utc) == old
        assert entitled(cid, old - timedelta(seconds=1)) and not entitled(cid, old)
        assert sum(ALERT_TEXT["subscription_cancelled"] in m for _, m in sent) == 1


@pytest.mark.parametrize("final", ["completed", "cancelled"])
def test_a_finished_subscription_never_extends_the_period(app_on, fake, final):
    old = end_in(5)
    with app_on() as c:
        cid = activate(c, fake, old)
        fake.subs["SUB_1"].update(status=final, next_payment_date=iso(end_in(40)))
        assert post_event(c, {"event": "subscription.disable", "data": {"subscription_code": "SUB_1"}}).status_code == 200
        st, end, _, _ = row(cid)
        assert st == "cancelled" and end.replace(tzinfo=timezone.utc) == old


def test_a_refund_is_recorded_and_alerted_but_never_revokes(app_on, fake, sent):
    with app_on() as c:
        cid = activate(c, fake, end_in(20))
        fake.txs["mbasub_ref1"]["status"] = "reversed"
        assert post_event(c, {"event": "refund.processed", "data": {"transaction_reference": "mbasub_ref1"}}).status_code == 200
        assert entitled(cid) and row(cid)[0] == "active"
        with session_scope() as s:
            assert s.query(BillingPayment).one().status == "refunded"
        assert post_event(c, {"event": "refund.processed", "data": {"transaction_reference": "nope"}}).status_code == 200
    assert any(ALERT_TEXT["refund_seen"] in m for _, m in sent) and any(ALERT_TEXT["refund_unmatched"] in m for _, m in sent)


def test_a_subscription_on_another_plan_or_email_is_not_trusted(app_on, fake, sent):
    old = end_in(20)
    with app_on() as c:
        cid = activate(c, fake, old)
        fake.subs["SUB_1"]["status"] = "non-renewing"
        fake.subs["SUB_1"]["plan"]["plan_code"] = "PLN_other"
        post_event(c, {"event": "subscription.disable", "data": {"subscription_code": "SUB_1"}})
        assert row(cid)[0] == "active"
        fake.subs["SUB_1"]["plan"]["plan_code"] = PLAN
        fake.subs["SUB_1"]["customer"]["email"] = "someone@else.example"
        post_event(c, {"event": "subscription.disable", "data": {"subscription_code": "SUB_1"}})
        assert row(cid)[0] == "active"
        fake.subs["SUB_1"]["customer"]["email"] = "payer@example.com"
        fake.subs["SUB_1"]["status"] = "mystery"
        post_event(c, {"event": "subscription.disable", "data": {"subscription_code": "SUB_1"}})
        assert row(cid)[0] == "active"
    assert len(sent) == 3


# ====================================================================== tolerance and coexistence
@pytest.mark.parametrize("event", [
    {"event": "charge.success"}, {"event": "charge.success", "data": None}, {"event": "charge.success", "data": []},
    {"event": "subscription.create"}, {"event": "subscription.create", "data": "x"}, {"event": "subscription.disable", "data": {}},
    {"event": "invoice.payment_failed", "data": {"customer": None, "subscription": 5}}, {"event": "refund.processed", "data": {}},
    {"event": 7}, {"data": {}}, {"event": "subscription.expiring_cards", "data": [1, 2]}, {"event": "invoice.create", "data": {}},
    {"event": "subscription.create", "data": {"subscription_code": 5, "customer": {"email": 5}}},
])
def test_odd_or_missing_fields_never_crash_and_change_nothing(app_on, fake, sent, event):
    with app_on() as c:
        signed_up(c)
        assert post_event(c, event).status_code == 200
        assert count(Subscription) == 0 and count(BillingPayment) == 0


def test_the_other_customers_state_is_not_touched(app_on, fake):
    with app_on() as c:
        a = activate(c, fake, end_in(10))
        b = signed_up(c, "other@example.com")
        with session_scope() as s:
            s.get(Customer, b).has_paid = True
        fake.subs["SUB_1"]["status"] = "non-renewing"
        post_event(c, {"event": "subscription.disable", "data": {"subscription_code": "SUB_1"}})
        assert row(b) is None and entitled(b)


def test_resubscribing_after_a_cancellation_uses_the_new_subscription(app_on, fake):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        fake.subs["SUB_1"]["status"] = "non-renewing"
        post_event(c, {"event": "subscription.disable", "data": {"subscription_code": "SUB_1"}})
        assert row(cid)[0] == "cancelled"
        issue(cid, "mbasub_ref2")
        fake.tx("mbasub_ref2", cid, "payer@example.com")
        new_end = end_in(30)
        fake.sub("SUB_2", "payer@example.com", new_end)
        assert post_event(c, charge_event("mbasub_ref2")).status_code == 200
        st, end, ref, _ = row(cid)
        assert (st, ref) == ("active", "SUB_2") and end.replace(tzinfo=timezone.utc) == new_end


# ====================================================================== static guarantees
SRC = Path(__file__).resolve().parent.parent / "src" / "mba_bot"


def test_the_period_end_is_never_computed_by_us():
    text = (SRC / "billing" / "subscriptions.py").read_text() + (SRC / "billing" / "paystack_sub.py").read_text()
    for banned in ("relativedelta", "monthrange", "days=30", "days=31", "timedelta(weeks", "+ 30", "months="):
        assert banned not in text
    tree = ast.parse((SRC / "billing" / "subscriptions.py").read_text())
    for node in ast.walk(tree):  # current_period_end is only ever assigned Paystack's parsed value
        if isinstance(node, ast.keyword) and node.arg == "current_period_end":
            assert isinstance(node.value, ast.Name) and node.value.id == "new_end"


def test_no_trading_or_credential_code_is_imported():
    for name in ("subscriptions.py", "paystack_sub.py"):
        tree = ast.parse((SRC / "billing" / name).read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | \
               {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m and any(x in m for x in ("venue", "lifecycle", "exchange", "trader", "risk", "onboarding")) for m in mods), mods


def test_no_live_key_or_provider_secret_is_in_the_source():
    for name in ("subscriptions.py", "paystack_sub.py"):
        assert not re.search(r"[sp]k_(live|test)_[A-Za-z0-9]{6,}", (SRC / "billing" / name).read_text())


def test_the_new_migration_is_one_idempotent_table_on_top_of_stage_0(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = next((SRC.parent.parent / "alembic" / "versions").glob("a7d4e1c9b3f5_*.py"))
    spec = importlib.util.spec_from_file_location("sub_tokens_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "f6c3a9d1b8e2"
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    with eng.begin() as conn:
        conn.execute(text("create table subscriptions (id varchar(36) primary key)"))

    def run(fn):
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    run(mig.upgrade)
    run(mig.upgrade)  # idempotent
    assert inspect(eng).get_table_names() == ["subscription_tokens", "subscriptions"]
    run(mig.downgrade)
    assert inspect(eng).get_table_names() == ["subscriptions"]
