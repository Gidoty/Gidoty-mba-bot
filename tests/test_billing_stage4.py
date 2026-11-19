"""Billing Stage 4 (docs/security/billing_paystack_subscriptions.md, "Stage 4"): the new-customer Activate screen offers a monthly subscription, and the lifetime payment is no longer sold,
only while SUBSCRIPTION_BILLING_ENABLED is on.

PROVES: with the switch off (unset or 0) the lifetime route, the subscription and crypto routes and the banner route answer exactly what they answered before and the new options route does not exist (404);
with it on a new customer gets the options from the server settings (price from PAYSTACK_PLAN_AMOUNT and NOWPAYMENTS_PRICE_USD, crypto only when NOWPAYMENTS_ENABLED and configured), the lifetime route is
refused for anyone who is not already has_paid, a legacy lifetime customer is never written to and still gets the old answers, the states active, grace, expired and cancelled are reported by the one
entitlement rule, and nothing secret, no payment reference and no provider link is returned or logged beyond the checkout URL.
DOES NOT PROVE: real Paystack or NOWPayments behaviour, the screens in a browser."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event, func, select

from admin_support import client_for, mk_customer
from mba_bot import db as dbmod
from mba_bot.api.routers import billing
from mba_bot.api.routers.billing import LIFETIME_NOT_SOLD
from mba_bot.config import load_settings
from mba_bot.db import session_scope
from mba_bot.models import BillingPayment, Customer, PaymentReference, Subscription

CANARIES = {"PAYSTACK_SECRET_KEY": "sk_test_CANARYSECRETKEY0001", "PAYSTACK_PLAN_CODE": "PLN_canaryplan0001", "NOWPAYMENTS_API_KEY": "CANARY-NOWPAYMENTS-API-KEY", "NOWPAYMENTS_IPN_SECRET": "CANARY-IPN-SECRET",
            "NOWPAYMENTS_IPN_URL": "https://canary-host.example/api/billing/nowpayments/ipn"}
NOW = datetime.now(timezone.utc)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/b4.db")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-stage4")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "mba_test_bot")
    for k in ("SUBSCRIPTION_BILLING_ENABLED", "NOWPAYMENTS_ENABLED", "PAYSTACK_PLAN_AMOUNT", "NOWPAYMENTS_PRICE_USD", *CANARIES, "NOWPAYMENTS_PAY_CURRENCY", "ADMIN_DASHBOARD_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    dbmod.init_db(s.database_url)
    from mba_bot.api.app import create_app

    return type("Env", (), {"app": create_app(), "settings": s, "mp": monkeypatch})()


def configure(env, *, billing_on=True, paystack=True, crypto=False, plan_amount="1234500", price="7.5"):
    mp = env.mp
    if billing_on:
        mp.setenv("SUBSCRIPTION_BILLING_ENABLED", "1")
    if paystack:
        mp.setenv("PAYSTACK_SECRET_KEY", CANARIES["PAYSTACK_SECRET_KEY"])
        mp.setenv("PAYSTACK_PLAN_CODE", CANARIES["PAYSTACK_PLAN_CODE"])
        mp.setenv("PAYSTACK_PLAN_AMOUNT", plan_amount)
    if crypto:
        mp.setenv("NOWPAYMENTS_ENABLED", "1")
        mp.setenv("NOWPAYMENTS_API_KEY", CANARIES["NOWPAYMENTS_API_KEY"])
        mp.setenv("NOWPAYMENTS_IPN_SECRET", CANARIES["NOWPAYMENTS_IPN_SECRET"])
        mp.setenv("NOWPAYMENTS_IPN_URL", CANARIES["NOWPAYMENTS_IPN_URL"])
        mp.setenv("NOWPAYMENTS_PAY_CURRENCY", "usdtbsc")
    mp.setenv("NOWPAYMENTS_PRICE_USD", price)


def customer(paid=False, email=None):
    with session_scope() as s:
        c = mk_customer(s, email, paid=paid)
        return c.id


def add_sub(cid, *, status="active", end_hours=24, provider="paystack", grace_hours=None):
    with session_scope() as s:
        s.add(Subscription(customer_id=cid, status=status, current_period_end=NOW + timedelta(hours=end_hours), grace_until=None if grace_hours is None else NOW + timedelta(hours=grace_hours),
                           provider=provider, provider_subscription_ref="SUB_CANARY_REF_9"))


class _Resp:
    status_code = 200

    def __init__(self, ref):
        self.ref = ref

    def json(self):
        return {"status": True, "data": {"authorization_url": "https://checkout.example/pay/abc", "reference": self.ref}}

    is_error = False


def fake_paystack(env, calls):
    def post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append((url, json))
        return _Resp((json or {}).get("reference", "ref_lifetime_1"))

    env.mp.setattr(billing.httpx, "post", post)


# ------------------------------------------------------------------------------------------------ switch off: exactly as before
@pytest.mark.parametrize("flag", [None, "0", "false", ""])
def test_switch_off_every_billing_answer_is_what_it_was(env, flag):
    if flag is not None:
        env.mp.setenv("SUBSCRIPTION_BILLING_ENABLED", flag)
    cid, paid = customer(), customer(paid=True)
    c, p = client_for(env.app, cid, env.settings), client_for(env.app, paid, env.settings)
    # no Paystack key: the lifetime route answers what it always answered
    r = c.post("/api/billing/initialize")
    assert (r.status_code, r.json()) == (503, {"detail": "PAYSTACK_SECRET_KEY is not configured"})
    env.mp.setenv("PAYSTACK_SECRET_KEY", CANARIES["PAYSTACK_SECRET_KEY"])
    calls = []
    fake_paystack(env, calls)
    r = c.post("/api/billing/initialize")
    assert (r.status_code, r.json()) == (200, {"authorization_url": "https://checkout.example/pay/abc"}) and len(calls) == 1 and calls[0][1]["amount"] == env.settings.paystack_amount  # the lifetime payment, as before
    r = p.post("/api/billing/initialize")
    assert (r.status_code, r.json()) == (409, {"detail": "this customer already has lifetime access"})
    for path, method in (("/api/billing/subscription/initialize", "post"), ("/api/billing/crypto/initialize", "post"), ("/api/billing/options", "get")):
        r = getattr(c, method)(path)
        assert (r.status_code, r.json()) == (404, {"detail": "Not Found"}), path
    assert c.get("/api/billing/notice").json() == {"notice": None}
    assert LIFETIME_NOT_SOLD not in json.dumps([c.post("/api/billing/initialize").text])


def test_switch_off_the_options_route_does_not_exist_for_a_signed_out_visitor_either(env):
    from fastapi.testclient import TestClient

    assert TestClient(env.app, base_url="https://testserver").get("/api/billing/options").status_code in (401, 404)


# ------------------------------------------------------------------------------------------------ switch on: a new customer
def test_a_new_customer_gets_the_options_from_the_server_settings(env):
    configure(env, crypto=True, plan_amount="1234500", price="7.5")
    cid = customer()
    r = client_for(env.app, cid, env.settings).get("/api/billing/options")
    assert r.status_code == 200
    assert r.json() == {
        "state": "none", "provider": None, "period_end": None, "grace_end": None, "grace_days": 5,
        "card": {"available": True, "can_start": True, "amount_minor": 1234500, "currency": "NGN"},
        "crypto": {"available": True, "can_start": True, "price_usd": "7.50"},
    }
    configure(env, crypto=False, plan_amount="2000000", price="12")
    env.mp.setenv("NOWPAYMENTS_ENABLED", "")
    body = client_for(env.app, cid, env.settings).get("/api/billing/options").json()
    assert body["card"]["amount_minor"] == 2000000 and body["crypto"] == {"available": False, "can_start": False, "price_usd": "12.00"}  # the price follows the setting, not the screen


def test_crypto_is_hidden_unless_enabled_and_configured(env):
    cid = customer()
    c = lambda: client_for(env.app, cid, env.settings)
    configure(env, crypto=False)
    assert c().get("/api/billing/options").json()["crypto"]["available"] is False
    assert c().post("/api/billing/crypto/initialize").status_code == 404
    env.mp.setenv("NOWPAYMENTS_ENABLED", "1")  # enabled but nothing configured: no button that would answer 503
    assert c().get("/api/billing/options").json()["crypto"] == {"available": False, "can_start": False, "price_usd": "7.50"}
    configure(env, crypto=True)
    assert c().get("/api/billing/options").json()["crypto"]["available"] is True
    env.mp.setenv("NOWPAYMENTS_ENABLED", "0")
    assert c().get("/api/billing/options").json()["crypto"]["available"] is False


def test_card_is_not_offered_until_paystack_is_configured(env):
    configure(env, paystack=False)
    r = client_for(env.app, customer(), env.settings).get("/api/billing/options").json()
    assert r["card"]["available"] is False and r["card"]["can_start"] is False


def test_the_card_checkout_route_is_the_subscription_route_and_returns_only_the_checkout_url(env):
    configure(env)
    calls = []
    fake_paystack(env, calls)
    r = client_for(env.app, customer(), env.settings).post("/api/billing/subscription/initialize")
    assert r.status_code == 200 and r.json() == {"authorization_url": "https://checkout.example/pay/abc"}
    assert calls[0][1]["plan"] == CANARIES["PAYSTACK_PLAN_CODE"] and "amount" not in calls[0][1]  # the plan decides the price


# ------------------------------------------------------------------------------------------------ the lifetime route is refused while on
def test_the_lifetime_route_is_refused_for_a_customer_who_is_not_already_paid(env, caplog):
    configure(env)
    calls = []
    fake_paystack(env, calls)
    cid = customer()
    with session_scope() as s:
        before = (s.get(Customer, cid).paystack_reference, s.scalar(select(func.count()).select_from(PaymentReference)), s.scalar(select(func.count()).select_from(BillingPayment)))
    with caplog.at_level(logging.DEBUG):
        r = client_for(env.app, cid, env.settings).post("/api/billing/initialize")
    assert r.status_code == 409 and r.json() == {"detail": LIFETIME_NOT_SOLD}
    assert calls == []  # Paystack was never called
    with session_scope() as s:
        assert (s.get(Customer, cid).paystack_reference, s.scalar(select(func.count()).select_from(PaymentReference)), s.scalar(select(func.count()).select_from(BillingPayment))) == before
    assert CANARIES["PAYSTACK_SECRET_KEY"] not in caplog.text
    # it is refused even before Paystack is configured: the answer does not depend on the key
    env.mp.delenv("PAYSTACK_SECRET_KEY")
    assert client_for(env.app, cid, env.settings).post("/api/billing/initialize").json() == {"detail": LIFETIME_NOT_SOLD}


def test_an_expired_subscriber_cannot_buy_lifetime_either_but_can_subscribe_again(env):
    configure(env)
    cid = customer()
    add_sub(cid, status="active", end_hours=-24 * 9)
    c = client_for(env.app, cid, env.settings)
    assert c.post("/api/billing/initialize").json() == {"detail": LIFETIME_NOT_SOLD}
    fake_paystack(env, [])
    assert c.post("/api/billing/subscription/initialize").status_code == 200


# ------------------------------------------------------------------------------------------------ switch on: a legacy lifetime customer is untouched
def test_a_legacy_lifetime_customer_is_never_written_to_and_keeps_the_old_answers(env):
    configure(env, crypto=True)
    cid = customer(paid=True)
    with session_scope() as s:
        snap = {c.name: getattr(s.get(Customer, cid), c.name) for c in Customer.__table__.columns}
    counted = []

    def watch(conn, cursor, statement, parameters, context, executemany):
        counted.append(statement.lstrip().split()[0].upper())

    event.listen(dbmod._engine, "before_cursor_execute", watch)
    try:
        fake_paystack(env, [])
        c = client_for(env.app, cid, env.settings)
        opts = c.get("/api/billing/options").json()
        assert opts["state"] == "legacy_lifetime" and opts["card"]["can_start"] is False and opts["crypto"]["can_start"] is False and opts["provider"] is None
        r = c.post("/api/billing/initialize")
        assert (r.status_code, r.json()) == (409, {"detail": "this customer already has lifetime access"})  # the old answer, not the new refusal
        assert c.post("/api/billing/subscription/initialize").status_code == 409
        assert c.post("/api/billing/crypto/initialize").status_code == 409
        assert c.get("/api/billing/notice").json() == {"notice": None}
    finally:
        event.remove(dbmod._engine, "before_cursor_execute", watch)
    assert not {"INSERT", "UPDATE", "DELETE"} & set(counted), counted
    with session_scope() as s:
        assert {c.name: getattr(s.get(Customer, cid), c.name) for c in Customer.__table__.columns} == snap
        assert s.scalar(select(func.count()).select_from(Subscription).where(Subscription.customer_id == cid)) == 0


def test_the_options_route_writes_nothing_for_anyone(env):
    configure(env, crypto=True)
    ids = [customer(), customer(paid=True)]
    for st, hrs in (("active", 24), ("active", -2), ("active", -24 * 9), ("cancelled", 24)):
        c = customer()
        add_sub(c, status=st, end_hours=hrs)
        ids.append(c)
    counted = []
    event.listen(dbmod._engine, "before_cursor_execute", lambda conn, cur, st, p, ctx, many: counted.append(st.lstrip().split()[0].upper()))
    for cid in ids:
        assert client_for(env.app, cid, env.settings).get("/api/billing/options").status_code == 200
    assert not {"INSERT", "UPDATE", "DELETE"} & set(counted)


# ------------------------------------------------------------------------------------------------ the states
@pytest.mark.parametrize("status,end_hours,grace_hours,provider,expected", [
    ("active", 48, None, "paystack", "active"),
    ("active", -24, None, "paystack", "grace"),            # inside the 5 day grace window
    ("active", -24 * 9, None, "paystack", "expired"),      # past it
    ("active", -24 * 9, 48, "nowpayments", "grace"),       # a stored later grace end wins
    ("cancelled", 48, None, "paystack", "cancelled"),
    ("cancelled", -1, None, "paystack", "expired"),
    ("weird", 48, None, "paystack", "expired"),            # fail closed
])
def test_subscription_states_come_from_the_entitlement_rule(env, status, end_hours, grace_hours, provider, expected):
    configure(env, crypto=True)
    cid = customer()
    add_sub(cid, status=status, end_hours=end_hours, provider=provider, grace_hours=grace_hours)
    body = client_for(env.app, cid, env.settings).get("/api/billing/options").json()
    assert body["state"] == expected and body["provider"] == provider and body["grace_days"] == 5
    assert body["period_end"] is not None and body["grace_end"] is not None
    # who may start a checkout follows the same rules as the checkout routes
    assert body["card"]["can_start"] is (expected in ("grace", "expired"))


def test_a_card_subscriber_cannot_start_crypto_while_the_card_renews_but_can_in_grace(env):
    configure(env, crypto=True)
    cid = customer()
    add_sub(cid, status="active", end_hours=48, provider="paystack")
    assert client_for(env.app, cid, env.settings).get("/api/billing/options").json()["crypto"]["can_start"] is False
    cid2 = customer()
    add_sub(cid2, status="active", end_hours=-24, provider="paystack")
    assert client_for(env.app, cid2, env.settings).get("/api/billing/options").json()["crypto"]["can_start"] is True


# ------------------------------------------------------------------------------------------------ nothing secret
ALLOWED_KEYS = {"state", "provider", "period_end", "grace_end", "grace_days", "card", "crypto"}


def test_the_options_answer_has_fixed_fields_and_no_secret_reference_or_link(env, caplog):
    configure(env, crypto=True)
    with session_scope() as s:
        s.add(BillingPayment(customer_id=customer(), provider="paystack", reference="mbasub_CANARYREFERENCE1", amount_minor=1, currency="NGN", status="paid"))
    texts = []
    with caplog.at_level(logging.DEBUG):
        for st, hrs in (("active", 24), ("active", -2), ("active", -24 * 9), ("cancelled", 24), (None, 0)):
            cid = customer()
            if st:
                add_sub(cid, status=st, end_hours=hrs)
            r = client_for(env.app, cid, env.settings).get("/api/billing/options")
            assert set(r.json()) == ALLOWED_KEYS and set(r.json()["card"]) == {"available", "can_start", "amount_minor", "currency"} and set(r.json()["crypto"]) == {"available", "can_start", "price_usd"}
            texts.append(r.text)
    answers = " ".join(texts)
    for forbidden in ("http", "checkout", "@", "reference"):  # no link, no e-mail, no payment reference in an answer
        assert forbidden not in answers.lower(), forbidden
    for secret in (*CANARIES.values(), "SUB_CANARY_REF_9", "mbasub_CANARYREFERENCE1"):
        assert secret not in answers + caplog.text, secret


def test_the_checkout_answers_carry_nothing_but_the_url_and_log_no_link(env, caplog):
    configure(env)
    fake_paystack(env, [])
    with caplog.at_level(logging.DEBUG):
        r = client_for(env.app, customer(), env.settings).post("/api/billing/subscription/initialize")
    assert set(r.json()) == {"authorization_url"}
    assert "checkout.example" not in caplog.text and CANARIES["PAYSTACK_SECRET_KEY"] not in caplog.text and CANARIES["PAYSTACK_PLAN_CODE"] not in caplog.text


def test_the_options_route_needs_a_signed_in_customer(env):
    from fastapi.testclient import TestClient

    configure(env)
    assert TestClient(env.app, base_url="https://testserver").get("/api/billing/options").status_code == 401


def test_a_row_governs_even_for_a_customer_who_also_has_the_lifetime_flag(env):
    configure(env)
    cid = customer(paid=True)
    add_sub(cid, status="active", end_hours=-24 * 9)  # the entitlement rule: a subscription row governs when it exists
    body = client_for(env.app, cid, env.settings).get("/api/billing/options").json()
    assert body["state"] == "expired" and body["card"]["can_start"] is True
    assert client_for(env.app, cid, env.settings).post("/api/billing/initialize").json() == {"detail": "this customer already has lifetime access"}  # the lifetime route still answers as it did
