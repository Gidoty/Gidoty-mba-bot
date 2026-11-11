"""Real Paystack TEST-mode bodies (tests/fixtures/paystack, captured 11 Nov 2026) run through the handlers.

Each test asserts the outcome the live test run produced: first payment (subscription.create arrived BEFORE charge.success in both
runs), a replay, a renewal whose charge carries no customer id in its metadata, invoice.create, and a cancel (non-renewing, date kept).

PROVES: the parsers and handlers read these real shapes and reach the live outcomes. DOES NOT PROVE: shapes that were not captured
(invoice.payment_failed, subscription.disable, subscription.expiring_cards, refund.*), Paystack's timing, or live mode. Paystack's
answers to our reads are replayed from the same capture, in the order they were given. No network."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from mba_bot import crypto_utils, protection_guard
from mba_bot.billing import paystack_sub as ps
from mba_bot.billing import subscriptions as subs
from mba_bot.billing.entitlement import is_entitled
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.models import BillingPayment, Customer, PaymentEvent, Subscription, SubscriptionToken

FIX = Path(__file__).parent / "fixtures" / "paystack"
EMAIL = "fixture.customer@example.com"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def fill(obj, email, cid):
    """Replace the {EMAIL} and {CUSTOMER_ID} placeholders anywhere in a decoded body."""
    if isinstance(obj, dict):
        return {k: fill(v, email, cid) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill(v, email, cid) for v in obj]
    if isinstance(obj, str):
        return obj.replace("{EMAIL}", email).replace("{CUSTOMER_ID}", cid)
    return obj


def at(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class Replay:
    """Paystack, answering exactly what it answered during the live run."""

    def __init__(self, fixture, email, cid):
        self.calls = []
        self.tx, self.subs, self.sub_idx = {}, [], 0
        for r in fixture["reads"]:
            answer, path = fill(r["answer"], email, cid), fill(r["get"], email, cid)
            if path.startswith("/transaction/verify/"):
                self.tx.setdefault(path.rsplit("/", 1)[1], answer)
            elif path.startswith("/subscription?"):
                self.list_path, self.list = path, answer
            elif path.startswith("/subscription/"):
                self.subs.append(answer)
            elif path.startswith("/plan/"):
                self.plan = answer
            elif path.startswith("/customer/"):
                self.cust = answer

    async def get_json(self, path, settings):
        self.calls.append(path)
        if path.startswith("/transaction/verify/"):
            return self.tx.get(path.rsplit("/", 1)[1])
        if path.startswith("/subscription?"):
            assert path == self.list_path.replace("&perPage=20", "&perPage=20"), path  # numeric plan id, then numeric customer id
            return self.list
        if path.startswith("/subscription/"):
            return self.subs[self.sub_idx]
        if path.startswith("/plan/"):
            return self.plan
        if path.startswith("/customer/"):
            return self.cust
        raise AssertionError(path)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    protection_guard.reset_for_tests()
    sent = []

    class Notifier:
        def __init__(self, token):
            pass

        async def send(self, target, message):
            sent.append((target, message))

    monkeypatch.setattr(subs, "TelegramNotifier", Notifier)

    def build(name):
        fx = load(name)
        plan_code = next(e for e in fx["events"] if e["event"] == "charge.success")["body"]["data"]["plan"]["plan_code"]
        for k, v in {"DATABASE_URL": f"sqlite:///{tmp_path}/fx.db", "ENCRYPTION_KEY": Fernet.generate_key().decode(), "JWT_SECRET": "x",
                     "SUBSCRIPTION_BILLING_ENABLED": "1", "PAYSTACK_PLAN_CODE": plan_code, "PAYSTACK_PLAN_AMOUNT": "1500000",
                     "PAYSTACK_SECRET_KEY": "sk_test_fakefakefakefake", "TELEGRAM_BOT_TOKEN": "1:fake", "BILLING_ALERT_CHAT_ID": "7"}.items():
            monkeypatch.setenv(k, v)
        init_db(f"sqlite:///{tmp_path}/fx.db")
        with session_scope() as s:
            c = Customer(email=EMAIL, password_hash="x")
            s.add(c)
            s.flush()
            cid = c.id
        first_ref = next(e for e in fx["events"] if e["event"] == "charge.success")["body"]["data"]["reference"]
        with session_scope() as s:  # what /subscription/initialize records before the checkout link exists
            s.add(BillingPayment(customer_id=cid, provider="paystack", reference=first_ref, amount_minor=1_500_000, currency="NGN", status="pending"))
        rp = Replay(fx, EMAIL, cid)
        monkeypatch.setattr(ps, "get_json", rp.get_json)
        events = [{"file": e["file"], "event": e["event"], "body": fill(e["body"], EMAIL, cid)} for e in fx["events"]]
        return SimpleEnv(fx, events, rp, cid, load_settings(), sent)

    return build


class SimpleEnv:
    def __init__(self, fx, events, rp, cid, settings, sent):
        self.fx, self.events, self.rp, self.cid, self.settings, self.sent = fx, events, rp, cid, settings, sent

    def ev(self, name, nth=0):
        return [e for e in self.events if e["event"] == name][nth]["body"]

    def run(self, body, now):
        with session_scope() as db:
            return asyncio.run(subs.handle_event(db, body, self.settings, now=now))

    def row(self):
        with session_scope() as s:
            r = s.query(Subscription).filter_by(customer_id=self.cid).first()
            return None if r is None else (r.status, r.current_period_end.replace(tzinfo=timezone.utc), r.provider_subscription_ref)

    def entitled(self, now):
        with session_scope() as s:
            return is_entitled(s.get(Customer, self.cid), settings=self.settings, now=now)

    def count(self, model):
        with session_scope() as s:
            return s.query(model).count()

    def payments(self):
        with session_scope() as s:
            return [(p.reference, p.status, p.customer_id, p.amount_minor) for p in s.query(BillingPayment).order_by(BillingPayment.issued_at)]


# ====================================================================== monthly plan
def test_monthly_first_payment_cancel_and_replay_match_the_live_run(env):
    e = env("monthly")
    created = e.ev("subscription.create")
    charge = e.ev("charge.success")
    cancel = e.ev("subscription.not_renew")
    t0 = at(created["data"]["createdAt"]) + timedelta(seconds=5)
    end = at(created["data"]["next_payment_date"])

    # live order: subscription.create FIRST, then charge.success; both answered 200
    assert e.run(created, t0) == {"ok": True}
    status, period_end, code = e.row()
    assert status == "active" and period_end == end and code == created["data"]["subscription_code"]  # the end is Paystack's own date
    assert [p[1] for p in e.payments()] == ["paid"] and e.count(PaymentEvent) == 1 and e.count(SubscriptionToken) == 1
    assert e.entitled(t0) and e.entitled(end - timedelta(seconds=1))
    # the amount Paystack charged was price + fee and our check accepted it
    tx = e.rp.tx[charge["data"]["reference"]]["data"]
    assert (tx["amount"], tx["requested_amount"], tx["fees"], tx["currency"]) == (1_532_995, 1_500_000, 32_995, "NGN")

    assert e.run(charge, t0) == {"ok": True, "duplicate": True}  # charge.success after the payment was already settled
    # replay of both: nothing changes
    before = (e.row(), e.payments(), e.count(PaymentEvent), e.count(SubscriptionToken))
    assert e.run(created, t0) == {"ok": True} and e.run(charge, t0) == {"ok": True, "duplicate": True}
    assert (e.row(), e.payments(), e.count(PaymentEvent), e.count(SubscriptionToken)) == before

    # cancel: status non-renewing, date kept -> our cancelled, entitled until the date, then not (and no grace after a cancel)
    e.rp.sub_idx = next(i for i, a in enumerate(e.rp.subs) if a["data"]["status"] == "non-renewing")
    assert cancel["data"]["status"] == "non-renewing"
    assert e.run(cancel, t0 + timedelta(minutes=8)) == {"ok": True}
    status, period_end, _ = e.row()
    assert status == "cancelled" and period_end == end
    assert e.entitled(end - timedelta(seconds=1)) and not e.entitled(end) and not e.entitled(end + timedelta(days=1))
    assert any(subs.ALERTS["subscription_cancelled"] in m for _, m in e.sent)  # the owner is told once


def test_the_first_payment_is_also_applied_when_charge_success_comes_first(env):
    e = env("monthly")
    t0 = at(e.ev("subscription.create")["data"]["createdAt"]) + timedelta(seconds=5)
    assert e.run(e.ev("charge.success"), t0) == {"ok": True}
    assert e.row()[0] == "active" and e.entitled(t0)
    assert e.run(e.ev("subscription.create"), t0) == {"ok": True}  # now a no-op
    assert e.count(PaymentEvent) == 1 and e.count(SubscriptionToken) == 1


# ====================================================================== hourly plan: renewal
def first_payment(e):
    created = e.ev("subscription.create")
    t0 = at(created["data"]["createdAt"]) + timedelta(seconds=5)
    e.run(created, t0)
    e.run(e.ev("charge.success", 0), t0)
    return t0


def renewal_index(e):
    return next(i for i, a in enumerate(e.rp.subs) if a["data"]["next_payment_date"].startswith("2026-11-11T07:17") and a["data"]["status"] == "active")


def test_a_renewal_without_a_customer_id_in_its_metadata_is_tied_to_the_customer_by_email_and_plan(env):
    e = env("hourly")
    t0 = first_payment(e)
    first_end = e.row()[1]
    assert first_end == at("2026-11-11T06:17:00.000Z")
    renewal = e.ev("charge.success", 1)
    assert "customer_id" not in renewal["data"]["metadata"] and renewal["data"]["metadata"] == {"invoice_action": "create"}  # the real shape
    assert renewal["data"]["reference"] != e.ev("charge.success", 0)["data"]["reference"]  # a Paystack reference, not one we issued
    assert renewal["data"]["plan"]["plan_code"] == e.settings.paystack_plan_code and renewal["data"]["customer"]["email"] == EMAIL
    with session_scope() as db:
        assert subs.is_subscription_charge(db, renewal["data"], e.settings)  # ours: by plan and by the e-mail of a customer with our subscription

    t1 = at(renewal["data"]["paid_at"]) + timedelta(seconds=12)
    e.rp.sub_idx = renewal_index(e)
    assert e.run(renewal, t1) == {"ok": True}
    status, new_end, _ = e.row()
    assert status == "active" and new_end == at("2026-11-11T07:17:00.000Z") > first_end  # Paystack's new next_payment_date
    pays = e.payments()
    assert len(pays) == 2 and all(p[1] == "paid" and p[2] == e.cid for p in pays)
    assert pays[1][0] == renewal["data"]["reference"] and pays[1][3] == 1_500_000  # checked against the price, paid 1,532,995 with the fee
    assert e.count(PaymentEvent) == 2 and e.entitled(t1) and e.entitled(new_end - timedelta(seconds=1))
    assert e.run(renewal, t1) == {"ok": True, "duplicate": True} and e.row()[1] == new_end  # replay


def test_the_renewal_is_not_applied_before_paystacks_record_has_moved(env):
    e = env("hourly")
    t0 = first_payment(e)
    old = e.row()
    renewal = e.ev("charge.success", 1)
    e.rp.sub_idx = 0  # Paystack still shows the old next_payment_date
    with session_scope() as db:
        with pytest.raises(subs.RetryLater):
            asyncio.run(subs.handle_event(db, renewal, e.settings, now=at(renewal["data"]["paid_at"])))
    assert e.row() == old and len(e.payments()) == 1


def test_a_renewal_for_an_unknown_email_or_another_plan_grants_nothing(env):
    e = env("hourly")
    first_payment(e)
    old = e.row()
    renewal = e.ev("charge.success", 1)
    t1 = at(renewal["data"]["paid_at"])
    e.rp.sub_idx = renewal_index(e)
    # same body, but Paystack says the charge belongs to a different e-mail: no customer matches, nothing changes
    stranger = copy.deepcopy(renewal)
    stranger["data"]["customer"]["email"] = "someone.else@example.com"
    assert e.run(stranger, t1) == {"ok": True} and e.row() == old and len(e.payments()) == 1
    # same body, but the verified transaction is for a different plan: refused by the plan check
    verified = e.rp.tx[renewal["data"]["reference"]]["data"]
    assert isinstance(verified["plan"], str)  # the real verify answer names the plan as a plain string (plan_object holds the object)
    verified["plan"] = "PLN_someone_elses_plan"
    assert e.run(renewal, t1) == {"ok": True}
    assert e.row() == old
    assert [p[1] for p in e.payments()] == ["paid", "failed"]


def test_invoice_create_is_ignored_and_reads_nothing(env):
    e = env("hourly")
    first_payment(e)
    before, calls = (e.row(), e.payments()), len(e.rp.calls)
    inv = e.ev("invoice.create")
    assert inv["data"]["transaction"]["reference"] == e.ev("charge.success", 1)["data"]["reference"]  # the real shape: reference is nested
    assert ps.event_reference(inv["data"]) == inv["data"]["transaction"]["reference"]
    assert e.run(inv, at(inv["data"]["createdAt"])) == {"ok": True, "ignored": "invoice.create"}
    assert (e.row(), e.payments()) == before and len(e.rp.calls) == calls


def test_hourly_cancel_keeps_the_renewed_date(env):
    e = env("hourly")
    first_payment(e)
    renewal = e.ev("charge.success", 1)
    e.rp.sub_idx = renewal_index(e)
    e.run(renewal, at(renewal["data"]["paid_at"]) + timedelta(seconds=12))
    cancel = e.ev("subscription.not_renew")
    e.rp.sub_idx = next(i for i, a in enumerate(e.rp.subs) if a["data"]["status"] == "non-renewing")
    assert e.run(cancel, at("2026-11-11T06:35:30.000Z")) == {"ok": True}
    status, end, _ = e.row()
    assert status == "cancelled" and end == at("2026-11-11T07:17:00.000Z")
    assert e.entitled(end - timedelta(seconds=1)) and not e.entitled(end)


# ====================================================================== the whole live sequence over HTTP
def test_the_hourly_sequence_over_http_answers_200_every_time(monkeypatch, tmp_path):
    """The same bodies through the real route: raw-byte signature, router dispatch, frozen clock. The live run had no 503."""
    import hashlib
    import hmac

    from test_security_hardening import SK, client_for, make_app  # noqa: F401

    fx = load("hourly")
    plan_code = next(e for e in fx["events"] if e["event"] == "charge.success")["body"]["data"]["plan"]["plan_code"]
    protection_guard.reset_for_tests()
    sent = []

    class Notifier:
        def __init__(self, token):
            pass

        async def send(self, target, message):
            sent.append((target, message))

    monkeypatch.setattr(subs, "TelegramNotifier", Notifier)

    class Clock(datetime):
        value = None

        @classmethod
        def now(cls, tz=None):
            return cls.value

    monkeypatch.setattr(subs, "datetime", Clock)
    import asyncio as _a
    from mba_bot.api.app import create_app  # noqa: F401
    from mba_bot.api import app as appmod

    async def idle(settings):
        await _a.sleep(3600)

    for k, v in {"DATABASE_URL": f"sqlite:///{tmp_path}/h.db", "ENCRYPTION_KEY": Fernet.generate_key().decode(), "JWT_SECRET": "x", "COOKIE_SECURE": "false",
                 "POLL_INTERVAL_SECONDS": "3600", "PAYSTACK_SECRET_KEY": SK, "PAYSTACK_AMOUNT": "13500000", "PAYSTACK_CURRENCY": "NGN",
                 "MBA_BOT_LOCK_FILE": str(tmp_path / "l"), "SUBSCRIPTION_BILLING_ENABLED": "1", "PAYSTACK_PLAN_CODE": plan_code,
                 "PAYSTACK_PLAN_AMOUNT": "1500000", "TELEGRAM_BOT_TOKEN": "1:fake", "BILLING_ALERT_CHAT_ID": "7"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(appmod, "run_forever", idle)

    with client_for(create_app()) as c:
        r = c.post("/api/auth/signup", json={"email": EMAIL, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True})
        cid = r.json()["id"]
        first_ref = next(e for e in fx["events"] if e["event"] == "charge.success")["body"]["data"]["reference"]
        with session_scope() as s:
            s.add(BillingPayment(customer_id=cid, provider="paystack", reference=first_ref, amount_minor=1_500_000, currency="NGN", status="pending"))
        rp = Replay(fx, EMAIL, cid)
        monkeypatch.setattr(ps, "get_json", rp.get_json)

        def send(event, when, idx):
            Clock.value = at(when)
            rp.sub_idx = idx
            body = json.dumps(fill(event["body"], EMAIL, cid)).encode()
            return c.post("/api/billing/webhook", content=body, headers={"x-paystack-signature": hmac.new(SK.encode(), body, hashlib.sha512).hexdigest()})

        evs = fx["events"]
        non_renew = next(i for i, a in enumerate(rp.subs) if a["data"]["status"] == "non-renewing")
        answers = []
        answers.append(send(evs[0], "2026-11-11T05:17:20.000Z", 0))  # subscription.create
        answers.append(send(evs[1], "2026-11-11T05:17:25.000Z", 0))  # charge.success (first)
        answers.append(send(evs[2], "2026-11-11T06:30:27.000Z", renewal_index_of(rp)))  # charge.success (renewal)
        answers.append(send(evs[3], "2026-11-11T06:30:30.000Z", renewal_index_of(rp)))  # invoice.create
        answers.append(send(evs[4], "2026-11-11T06:35:21.000Z", non_renew))  # subscription.not_renew
        assert [a.status_code for a in answers] == [200] * 5
        assert [a.json() for a in answers] == [{"ok": True}, {"ok": True, "duplicate": True}, {"ok": True}, {"ok": True, "ignored": "invoice.create"}, {"ok": True}]
        with session_scope() as s:
            sub = s.query(Subscription).one()
            assert (sub.status, sub.current_period_end.replace(tzinfo=timezone.utc)) == ("cancelled", at("2026-11-11T07:17:00.000Z"))
            assert s.query(BillingPayment).filter_by(status="paid").count() == 2 and s.query(PaymentEvent).count() == 2
        # a signed replay of the renewal is a duplicate; a forged one is refused
        assert send(evs[2], "2026-11-11T06:40:00.000Z", renewal_index_of(rp)).json() == {"ok": True, "duplicate": True}
        body = json.dumps(fill(evs[2]["body"], EMAIL, cid)).encode()
        assert c.post("/api/billing/webhook", content=body, headers={"x-paystack-signature": "0" * 128}).status_code == 400


def renewal_index_of(rp):
    return next(i for i, a in enumerate(rp.subs) if a["data"]["next_payment_date"].startswith("2026-11-11T07:17") and a["data"]["status"] == "active")


# ====================================================================== what the fixtures contain
def test_the_fixtures_hold_no_secret_and_no_real_identifier():
    text = (FIX / "hourly.json").read_text() + (FIX / "monthly.json").read_text()
    assert not re.search(r"[sp]k_(live|test)_[A-Za-z0-9]{6,}|AUTH_[A-Za-z0-9]|SIG_[A-Za-z0-9]|whsec_", text)
    assert "@" not in text and "tok_" not in text
    for needle in ('"integration": ', ):
        assert all(v.strip().startswith("100000") for v in re.findall(r'"integration": ([^\n,]*)', text))
    assert len(re.findall(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", text)) == 0


def test_where_the_real_bodies_differ_from_a_naive_reading():
    """Pins the facts that matter for the parsers: written down so a Paystack change shows up as a failing test."""
    h = load("hourly")
    by = {}
    for ev in h["events"]:
        by.setdefault(ev["event"], []).append(ev["body"]["data"])
    first, renewal = by["charge.success"]
    assert first["metadata"]["customer_id"] == "{CUSTOMER_ID}" and renewal["metadata"] == {"invoice_action": "create"}
    assert "subscription_code" not in first and "subscription_code" not in renewal and "subscription" not in renewal  # a charge names no subscription
    assert by["subscription.create"][0]["subscription_code"] and by["subscription.not_renew"][0]["subscription_code"]  # subscription events do
    assert by["invoice.create"][0]["subscription"]["subscription_code"] and by["invoice.create"][0]["transaction"]["reference"]
    assert isinstance(first["plan"], dict) and first["plan"]["plan_code"] and first["requested_amount"] == 1_500_000
    assert first["authorization"]["reusable"] is True and renewal["authorization"]["channel"] == "card"
    assert ps.event_plan_code(renewal) == renewal["plan"]["plan_code"] and ps.event_customer_email(renewal) == "{EMAIL}"
    # the verify ANSWER differs from the webhook body: its `plan` is a plain string and the object is in `plan_object`
    verify = next(r["answer"]["data"] for r in h["reads"] if r["get"].startswith("/transaction/verify/"))
    assert isinstance(verify["plan"], str) and verify["plan_object"]["plan_code"] == verify["plan"] == first["plan"]["plan_code"]
    assert ps.event_plan_code(verify) == verify["plan"]  # read correctly in both shapes
    # subscription.create arrived before charge.success in both live runs
    assert [e["event"] for e in h["events"]][:2] == ["subscription.create", "charge.success"]
    assert [e["event"] for e in load("monthly")["events"]][:2] == ["subscription.create", "charge.success"]
