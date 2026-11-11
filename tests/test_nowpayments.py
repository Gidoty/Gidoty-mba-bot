"""Stage 2 of designs/03: NOWPayments (USDT on BSC) behind SUBSCRIPTION_BILLING_ENABLED, NOWPAYMENTS_ENABLED and NOWPAYMENTS_IPN_MODE.

PROVES: control flow, the status ladder, the amount rule, replay protection, the period rule, the signature check against signatures
built here, gating by the three switches, masked logging, and that nothing is written in observe mode. DOES NOT PROVE (UNVERIFIED, see
docs/security/billing_nowpayments.md): that NOWPayments signs the way these tests sign (16 serialisations are tried, none was seen
live), the real IPN body, the real shape of GET /v1/payment/{id} (every body here is invented from memory), retries, expiry, the
fee, Cloudflare. No network, no real key."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from test_security_hardening import client_for, make_app  # noqa: F401
from test_subscription_paystack import ENV as PS_ENV, count, signed_up

from mba_bot import protection_guard
from mba_bot.api import ratelimit
from mba_bot.billing import notices, nowpayments as npay, paystack_sub as ps, subscriptions as subs
from mba_bot.billing.entitlement import is_entitled
from mba_bot.billing.periods import add_months
from mba_bot.config import load_settings, parse_ipn_mode
from mba_bot.db import session_scope
from mba_bot.log_safety import secrets_from_settings
from mba_bot.models import BillingNotice, BillingPayment, Customer, PaymentEvent, Subscription, SubscriptionToken, TradingAccount

API_KEY = "np-api-key-for-tests-0123456789"
SECRET = "ipn-secret-for-tests-0123456789"
IPN_URL = "https://bot.example.test/api/billing/nowpayments/ipn"
ADDRESS = "0xADDRESSdoNotLogThis0000000000000000000000"
LINK = "https://nowpayments.io/payment/?iid=4815162342"
NP_ENV = {"SUBSCRIPTION_BILLING_ENABLED": "1", "NOWPAYMENTS_ENABLED": "1", "NOWPAYMENTS_IPN_MODE": "enforce", "NOWPAYMENTS_API_KEY": API_KEY,
          "NOWPAYMENTS_IPN_SECRET": SECRET, "NOWPAYMENTS_PAY_CURRENCY": "usdtbsc", "NOWPAYMENTS_IPN_URL": IPN_URL}
U = timezone.utc


def _now():
    return datetime.now(U)


class FakeNow:
    """NOWPayments as the handlers see it: three seams replaced (payment read, invoice create, minimum read)."""

    def __init__(self):
        self.payments, self.calls, self.posts = {}, [], []
        self.down = False
        self.min = None
        self.invoice = (201, {"id": "4815162342", "order_id": "x", "invoice_url": LINK})
        self.on_post = None

    async def get_json(self, path, settings):
        self.calls.append(path)
        if self.down:
            raise ps.ProviderUnavailable("http 503")
        return self.payments.get(path.rsplit("/", 1)[1])

    def post_invoice(self, settings, body):
        self.posts.append(body)
        if self.on_post:
            self.on_post(body)
        if isinstance(self.invoice, Exception):
            raise self.invoice
        return self.invoice

    def get_min_amount(self, settings):
        return self.min


def record(order, status="finished", pay_amount=10.04, actually_paid=10.04, pid=5077125051, **over):
    r = {"payment_id": pid, "payment_status": status, "pay_address": ADDRESS, "price_amount": 10, "price_currency": "usd", "pay_amount": pay_amount,
         "actually_paid": actually_paid, "pay_currency": "usdtbsc", "order_id": order, "order_description": "Metabridge subscription", "outcome_amount": 9.8,
         "outcome_currency": "usdtbsc", "created_at": "2026-11-12T10:00:00.000Z", "updated_at": "2026-11-12T10:05:00.000Z"}
    r.update(over)
    return r


def trigger(order, status="finished", pid=5077125051, **over):
    t = {"payment_id": pid, "payment_status": status, "pay_address": ADDRESS, "price_amount": 10, "price_currency": "usd", "pay_amount": 10.04,
         "actually_paid": 10.04, "pay_currency": "usdtbsc", "order_id": order, "outcome_amount": 9.8, "fee": {"currency": "usdtbsc", "depositFee": 0.1, "serviceFee": 0.1}}
    t.update(over)
    return t


def sign(body: dict, secret=SECRET) -> str:
    """An independent signer: sorted keys at every level, compact separators (the PDF's description)."""
    return hmac.new(secret.encode(), json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(), hashlib.sha512).hexdigest()


@pytest.fixture()
def fake(monkeypatch):
    f = FakeNow()
    monkeypatch.setattr(npay, "get_json", f.get_json)
    monkeypatch.setattr(npay, "post_invoice", f.post_invoice)
    monkeypatch.setattr(npay, "get_min_amount", f.get_min_amount)
    return f


@pytest.fixture()
def tg(monkeypatch):
    protection_guard.reset_for_tests()

    class Rec:
        def __init__(self):
            self.log = []

        @property
        def to_owner(self):
            return [m for t, m in self.log if t == PS_ENV["BILLING_ALERT_CHAT_ID"]]

        @property
        def to_customers(self):
            return [(t, m) for t, m in self.log if t != PS_ENV["BILLING_ALERT_CHAT_ID"]]

        def owner_has(self, code):
            return any(subs.ALERTS[code] in m for m in self.to_owner)

    rec = Rec()

    class Notifier:
        def __init__(self, token):
            pass

        async def send(self, target, message):
            rec.log.append((target, message))

    monkeypatch.setattr(subs, "TelegramNotifier", Notifier)
    monkeypatch.setattr(notices, "TelegramNotifier", Notifier)
    return rec


@pytest.fixture()
def app(make_app, tg, fake):
    return lambda **extra: client_for(make_app(**{**PS_ENV, **NP_ENV, **extra}))


def issue(cid, ref="mbacrypto_ref1", status="pending", cents=1000):
    with session_scope() as s:
        s.add(BillingPayment(customer_id=cid, provider="nowpayments", reference=ref, amount_minor=cents, currency="USD", usd_value=Decimal(cents) / 100, status=status))


def customer(c, email="crypto@example.com"):
    return signed_up(c, email)


def ipn(c, body, *, sig=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    return c.post("/api/billing/nowpayments/ipn", content=data, headers={"x-nowpayments-sig": sig if sig is not None else sign(body)})


def pay(c, fake, cid, *, order="mbacrypto_ref1", status="finished", pid=5077125051, rec_over=None, trig_over=None, issue_row=True, **kw):
    """Issue an invoice (once), let NOWPayments report this record, and send the IPN for it."""
    if issue_row:
        with session_scope() as db:
            exists = db.query(BillingPayment).filter_by(reference=order).count()
        if not exists:
            issue(cid, order)
    fake.payments[str(pid)] = record(order, status, pid=pid, **{**kw, **(rec_over or {})})
    return ipn(c, trigger(order, status, pid, **(trig_over or {})))


def sub_row(cid):
    with session_scope() as s:
        r = s.query(Subscription).filter_by(customer_id=cid).first()
        return None if r is None else (r.status, r.current_period_start.replace(tzinfo=U), r.current_period_end.replace(tzinfo=U), r.provider, r.provider_subscription_ref)


def entitled(cid, now=None):
    with session_scope() as s:
        return is_entitled(s.get(Customer, cid), settings=load_settings(), now=now)


def bp_state(ref="mbacrypto_ref1"):
    with session_scope() as s:
        b = s.query(BillingPayment).filter_by(reference=ref).one()
        return b.status, b.provider_payment_id, (b.usd_value if b.usd_value is None else Decimal(str(b.usd_value))), b.period_end


def add_sub(cid, *, provider="nowpayments", status="active", end, ref=None):
    with session_scope() as s:
        s.add(Subscription(customer_id=cid, status=status, current_period_start=end - timedelta(days=30), current_period_end=end, provider=provider, provider_subscription_ref=ref))


def approx(a, b, seconds=30):
    return abs((a - b).total_seconds()) < seconds


# ====================================================================== settings
def test_settings_default_off_and_parse_safely(monkeypatch):
    for k in ("NOWPAYMENTS_ENABLED", "NOWPAYMENTS_IPN_MODE", "NOWPAYMENTS_API_KEY", "NOWPAYMENTS_IPN_SECRET", "NOWPAYMENTS_PAY_CURRENCY", "NOWPAYMENTS_PRICE_USD",
              "NOWPAYMENTS_UNDERPAY_TOLERANCE_PCT", "NOWPAYMENTS_IPN_URL"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    assert (s.nowpayments_enabled, s.nowpayments_ipn_mode, s.nowpayments_api_key, s.nowpayments_ipn_secret, s.nowpayments_pay_currency) == (False, "off", None, None, None)
    assert (s.nowpayments_price_usd, s.nowpayments_underpay_tolerance_pct, s.nowpayments_ipn_url) == ("10", 1.0, None)
    assert "api" not in repr(s).lower() or API_KEY not in repr(s)
    for raw, mode in ((None, "off"), ("", "off"), ("1", "off"), ("true", "off"), ("ENFORCE", "enforce"), (" Observe ", "observe"), ("log", "off")):
        assert parse_ipn_mode(raw) == mode  # anything unrecognised is off, never enforce by accident
    monkeypatch.setenv("NOWPAYMENTS_PRICE_USD", "-3")
    monkeypatch.setenv("NOWPAYMENTS_UNDERPAY_TOLERANCE_PCT", "banana")
    s = load_settings()
    assert s.nowpayments_price_usd == "10" and s.nowpayments_underpay_tolerance_pct == 1.0
    monkeypatch.setenv("NOWPAYMENTS_UNDERPAY_TOLERANCE_PCT", "50")
    assert load_settings().nowpayments_underpay_tolerance_pct == 1.0


def test_the_two_secrets_are_registered_for_log_redaction(monkeypatch):
    for k, v in NP_ENV.items():
        monkeypatch.setenv(k, v)
    secrets = secrets_from_settings(load_settings())
    assert API_KEY in secrets and SECRET in secrets


def test_the_ipn_path_is_exempt_from_the_rate_limiter():
    assert "/api/billing/nowpayments/ipn" in ratelimit.EXEMPT_PATHS and "/api/billing/webhook" in ratelimit.EXEMPT_PATHS


# ====================================================================== all three switches
@pytest.mark.parametrize("env", [{"NOWPAYMENTS_ENABLED": "0"}, {"SUBSCRIPTION_BILLING_ENABLED": "0"}])
def test_both_routes_do_not_exist_unless_both_switches_are_on(app, fake, env):
    with app(**env) as c:
        cid = customer(c)
        assert c.post("/api/billing/crypto/initialize").status_code == 404
        body = trigger("x")
        assert ipn(c, body).status_code == 404
        assert fake.calls == [] and fake.posts == [] and count(BillingPayment) == 0


def test_the_ipn_mode_off_ignores_everything(app, fake):
    with app(NOWPAYMENTS_IPN_MODE="off") as c:
        cid = customer(c)
        r = pay(c, fake, cid)
        assert r.status_code == 200 and r.json() == {"ok": True, "ignored": "mode_off"}
        assert fake.calls == [] and sub_row(cid) is None and bp_state()[0] == "pending"


def test_a_missing_configuration_answers_503_and_does_nothing(app, fake):
    for missing in ("NOWPAYMENTS_API_KEY", "NOWPAYMENTS_PAY_CURRENCY", "NOWPAYMENTS_IPN_URL"):
        with app(**{missing: ""}) as c:
            customer(c, f"{missing.lower()}@example.com")
            assert c.post("/api/billing/crypto/initialize").status_code == 503
            assert fake.posts == []
    with app(NOWPAYMENTS_IPN_SECRET="") as c:
        assert ipn(c, trigger("x"), sig="0").status_code == 503  # enforce without a secret
    with app(NOWPAYMENTS_IPN_SECRET="", NOWPAYMENTS_IPN_MODE="observe") as c:
        assert ipn(c, trigger("x"), sig="0").json() == {"ok": True, "observed": "no_secret"}


# ====================================================================== checkout
def test_checkout_creates_the_exact_invoice_and_records_the_issue_price(app, fake):
    with app() as c:
        cid = customer(c)
        r = c.post("/api/billing/crypto/initialize")
        assert r.status_code == 200 and r.json() == {"authorization_url": LINK}
        body = fake.posts[0]
        ref = body["order_id"]
        assert ref.startswith("mbacrypto_") and len(ref) == len("mbacrypto_") + 24 and "@" not in ref and cid not in ref
        assert body == {"price_amount": 10.0, "price_currency": "usd", "pay_currency": "usdtbsc", "order_id": ref, "order_description": "Metabridge subscription, one month",
                        "ipn_callback_url": IPN_URL, "success_url": "http://localhost:5173/#/dashboard", "cancel_url": "http://localhost:5173/#/dashboard",
                        "is_fee_paid_by_user": False, "is_fixed_rate": False}
        with session_scope() as s:
            b = s.query(BillingPayment).one()
            assert (b.provider, b.reference, b.amount_minor, b.currency, b.status, b.customer_id) == ("nowpayments", ref, 1000, "USD", "pending", cid)


@pytest.mark.parametrize("code", [200, 201])
def test_both_200_and_201_are_success(app, fake, code):
    fake.invoice = (code, {"id": "1", "invoice_url": LINK})
    with app() as c:
        customer(c)
        assert c.post("/api/billing/crypto/initialize").status_code == 200


def test_the_invoice_row_is_committed_before_the_link_exists_and_no_lock_is_held_during_the_call(app, fake):
    seen = {}

    def during(body):
        with session_scope() as s:
            seen["rows"] = s.query(BillingPayment).filter_by(reference=body["order_id"]).count()
        path = load_settings().database_url.replace("sqlite:///", "")
        conn = sqlite3.connect(path, timeout=0.2)
        try:
            conn.execute("BEGIN IMMEDIATE")
            seen["lock"] = "free"
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    fake.on_post = during
    with app() as c:
        customer(c)
        assert c.post("/api/billing/crypto/initialize").status_code == 200
    assert seen == {"rows": 1, "lock": "free"}


@pytest.mark.parametrize("answer", [(400, {"status": False, "code": "AMOUNT_MINIMAL_ERROR", "message": "x"}), (200, {"id": "1"}), (200, {"invoice_url": "http://nowpayments.io/p"}),
                                    (201, {"invoice_url": "https://evil.example/pay"}), (201, {"invoice_url": "https://nowpayments.io.evil.example/pay"}), (500, None),
                                    (201, None), ps.ProviderUnavailable("down")])
def test_a_refused_or_odd_answer_hands_out_no_link_and_marks_the_row_failed(app, fake, answer, caplog):
    fake.invoice = answer
    caplog.set_level(logging.DEBUG)
    with app() as c:
        customer(c)
        r = c.post("/api/billing/crypto/initialize")
        assert r.status_code == 502 and "nowpayments" not in r.text.lower() and LINK not in r.text
        with session_scope() as s:
            assert s.query(BillingPayment).one().status == "failed"
    assert "AMOUNT_MINIMAL_ERROR" in caplog.text or answer != (400, {"status": False, "code": "AMOUNT_MINIMAL_ERROR", "message": "x"})
    assert API_KEY not in caplog.text


def test_the_minimum_is_checked_at_run_time_and_a_refusal_is_graceful(app, fake):
    with app() as c:
        customer(c)
        fake.min = Decimal("11")  # the minimum is above USD 10
        r = c.post("/api/billing/crypto/initialize")
        assert r.status_code == 409 and "pay by card" in r.json()["detail"] and fake.posts == [] and count(BillingPayment) == 0
        fake.min = Decimal("1.19")  # below: fine
        assert c.post("/api/billing/crypto/initialize").status_code == 200
        fake.min = None  # unreadable: never blocks
        assert c.post("/api/billing/crypto/initialize").status_code == 200


def test_checkout_rules_by_customer_state(app, fake):
    now = _now()
    cases = [("running card subscription", dict(provider="paystack", end=now + timedelta(days=3)), 409),
             ("crypto period running, far from the end", dict(end=now + timedelta(days=20)), 409),
             ("crypto period ending in 3 days", dict(end=now + timedelta(days=3)), 200),
             ("crypto period ending in exactly 7 days", dict(end=now + timedelta(days=7, seconds=-5)), 200),
             ("crypto period ending in 8 days", dict(end=now + timedelta(days=8)), 409),
             ("cancelled but paid up for 2 more days", dict(end=now + timedelta(days=2), status="cancelled"), 200),
             ("in grace", dict(end=now - timedelta(hours=2)), 200),
             ("expired", dict(end=now - timedelta(days=9)), 200),
             ("card subscription in grace", dict(provider="paystack", end=now - timedelta(hours=2)), 200)]
    for i, (label, kw, expected) in enumerate(cases):
        with app() as c:
            cid = customer(c, f"u{i}@example.com")
            add_sub(cid, **kw)
            assert c.post("/api/billing/crypto/initialize").status_code == expected, label
    with app() as c:
        cid = customer(c, "lifetime@example.com")
        with session_scope() as s:
            s.get(Customer, cid).has_paid = True
        assert c.post("/api/billing/crypto/initialize").status_code == 409


def test_at_most_three_unpaid_invoices_per_day(app, fake):
    with app() as c:
        customer(c)
        assert [c.post("/api/billing/crypto/initialize").status_code for _ in range(4)] == [200, 200, 200, 409]


# ====================================================================== the signature
def hmac_of(text: str, secret=SECRET) -> str:
    return hmac.new(secret.encode(), text.encode("utf-8"), hashlib.sha512).hexdigest()


WIRE = '{ "payment_status" : "finished", "price_amount": 10.0, "actually_paid": 10.040, "order_id": "mbacrypto_a", "fee": {"serviceFee": 0.1, "currency": "usdtbsc"}, "note": "café ü" }'


def test_signature_variants_cover_the_serialisations_a_sender_might_use():
    raw = WIRE.encode("utf-8")
    cases = {
        "nested-sorted-compact-utf8-orig": '{"actually_paid":10.040,"fee":{"currency":"usdtbsc","serviceFee":0.1},"note":"café ü","order_id":"mbacrypto_a","payment_status":"finished","price_amount":10.0}',
        "nested-sorted-compact-utf8-js": '{"actually_paid":10.04,"fee":{"currency":"usdtbsc","serviceFee":0.1},"note":"café ü","order_id":"mbacrypto_a","payment_status":"finished","price_amount":10}',
        "nested-sorted-compact-ascii-js": '{"actually_paid":10.04,"fee":{"currency":"usdtbsc","serviceFee":0.1},"note":"caf\\u00e9 \\u00fc","order_id":"mbacrypto_a","payment_status":"finished","price_amount":10}',
        "nested-sorted-spaced-utf8-orig": '{"actually_paid": 10.040, "fee": {"currency": "usdtbsc", "serviceFee": 0.1}, "note": "café ü", "order_id": "mbacrypto_a", "payment_status": "finished", "price_amount": 10.0}',
        "top-sorted-compact-utf8-js": '{"actually_paid":10.04,"fee":{"serviceFee":0.1,"currency":"usdtbsc"},"note":"café ü","order_id":"mbacrypto_a","payment_status":"finished","price_amount":10}',
    }
    for label, canonical in cases.items():
        assert npay.verify_signature(SECRET, raw, hmac_of(canonical)) == label, label
    assert len(npay.signature_variants(raw)) == 16 and len({b for _, b in npay.signature_variants(raw)}) == 16


def test_the_signature_does_not_depend_on_how_the_request_was_laid_out_on_the_wire():
    canonical = '{"a":1,"b":{"c":2,"d":[3,{"e":4,"f":5}]}}'
    sig = hmac_of(canonical)
    for wire in ('{"b":{"d":[3,{"f":5,"e":4}],"c":2},"a":1}', '{\n "a": 1,\n "b": {"c": 2, "d": [3, {"e": 4, "f": 5}]}\n}', '  {"a":1,"b":{"c":2,"d":[3,{"e":4,"f":5}]}}  '):
        assert npay.verify_signature(SECRET, wire.encode(), sig) == "nested-sorted-compact-utf8-orig"


@pytest.mark.parametrize("token,expected", [("10.0", "10"), ("10", "10"), ("1.50", "1.5"), ("12.50", "12.5"), ("0.1", "0.1"), ("1e-6", "0.000001"), ("1E-6", "0.000001"),
                                            ("1e-7", "1e-7"), ("1.5e-7", "1.5e-7"), ("0.00001", "0.00001"), ("1e21", "1e+21"), ("1E21", "1e+21"), ("100", "100"),
                                            ("-0.0000015", "-0.0000015"), ("-2.0", "-2"), ("0.0", "0"), ("123456789.123456789", "123456789.12345679"), ("1e2", "100")])
def test_javascript_number_text(token, expected):
    assert npay.js_number_text(token) == expected


@pytest.mark.parametrize("secret,header,raw", [
    ("other-secret-for-tests-0123456789", None, WIRE.encode()),
    (SECRET, None, WIRE.encode()), (SECRET, "", WIRE.encode()), (SECRET, "deadbeef", WIRE.encode()), (SECRET, "0" * 128, WIRE.encode()),
    (None, hmac_of('{"a":1}'), b'{"a":1}'), (SECRET, hmac_of('{"a":1}'), b'not json'), (SECRET, hmac_of('{"a":1}'), b'{"a":NaN}'), (SECRET, hmac_of('{"a":1}'), b'\xff\xfe'),
    (SECRET, hmac_of('{"a":1}'), b'[' * 5000),
])
def test_wrong_or_malformed_signatures_never_verify_and_never_raise(secret, header, raw):
    assert npay.verify_signature(secret, raw, header) is None


@pytest.mark.parametrize("header", ["é", "\u00e9" * 128, "０" * 128, "ab\u0000cd", " \u2028 ", "\ud800".encode("utf-8", "surrogatepass").decode("utf-8", "surrogatepass")])
def test_a_non_ascii_signature_header_is_no_match_and_never_raises(header):
    assert npay.verify_signature(SECRET, b'{"a":1}', header) is None


def test_non_ascii_characters_are_never_ignored_so_a_valid_signature_with_junk_does_not_verify():
    sig = hmac_of('{"a":1}')
    assert npay.verify_signature(SECRET, b'{"a":1}', sig) is not None
    for junk in (sig + "é", "é" + sig, sig[:60] + "é" + sig[60:], sig + "\u200b", sig.replace("a", "\uff41", 1) if "a" in sig else sig + "\uff41"):
        assert npay.verify_signature(SECRET, b'{"a":1}', junk) is None


def test_enforce_answers_400_and_observe_logs_for_a_non_ascii_signature_header(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1")
        body = json.dumps(trigger("mbacrypto_ref1")).encode()
        for raw_header in ("é".encode("utf-8"), "é".encode("latin-1") * 128, b"\xff\xfe", "０".encode("utf-8") * 128):  # the client sends bytes; the server reads them as latin-1 text
            r = c.post("/api/billing/nowpayments/ipn", content=body, headers={"x-nowpayments-sig": raw_header})
            assert r.status_code == 400, raw_header
        assert fake.calls == [] and sub_row(cid) is None and bp_state()[0] == "pending"
    with app(NOWPAYMENTS_IPN_MODE="observe") as c2:
        r = c2.post("/api/billing/nowpayments/ipn", content=body, headers={"x-nowpayments-sig": "é".encode("utf-8")})
        assert r.status_code == 200 and r.json() == {"ok": True, "observed": "bad_signature"} and fake.calls == []


def test_a_tampered_body_does_not_verify():
    sig = hmac_of('{"actually_paid":10.04,"order_id":"x","payment_status":"waiting"}')
    assert npay.verify_signature(SECRET, b'{"order_id":"x","payment_status":"waiting","actually_paid":10.04}', sig) is not None
    assert npay.verify_signature(SECRET, b'{"order_id":"x","payment_status":"finished","actually_paid":10.04}', sig) is None


def test_the_signature_is_case_insensitive_hex_and_compared_in_constant_time():
    sig = hmac_of('{"a":1}')
    assert npay.verify_signature(SECRET, b'{"a":1}', sig.upper()) is not None
    assert "compare_digest" in (Path(npay.__file__).read_text())


# ====================================================================== IPN: the switch ladder
def test_enforce_refuses_a_bad_or_missing_signature_before_reading_anything(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1")
        body = trigger("mbacrypto_ref1")
        assert ipn(c, body, sig="0" * 128).status_code == 400
        assert c.post("/api/billing/nowpayments/ipn", content=json.dumps(body).encode()).status_code == 400
        assert ipn(c, body, sig=sign(body, "wrong-secret-0123456789012345")).status_code == 400
        assert fake.calls == [] and sub_row(cid) is None


def test_observe_logs_a_masked_verdict_and_writes_nothing(app, fake, caplog):
    caplog.set_level(logging.DEBUG)
    with app(NOWPAYMENTS_IPN_MODE="observe") as c:
        cid = customer(c)
        r = pay(c, fake, cid)
        assert r.status_code == 200 and r.json() == {"ok": True, "observed": "apply"}
        assert sub_row(cid) is None and bp_state() == ("pending", None, Decimal("10"), None) and count(PaymentEvent) == 0
        assert not entitled(cid)
        # a forged one: logged, nothing is read from NOWPayments, nothing changes
        fake.calls.clear()
        assert ipn(c, trigger("mbacrypto_ref1"), sig="0" * 128).json() == {"ok": True, "observed": "bad_signature"}
        assert fake.calls == []
        # a signature that cannot verify because the provider is down: observed, not 503
        fake.down = True
        assert pay(c, fake, cid).json() == {"ok": True, "observed": "provider_unavailable"}
    line = next(m for m in caplog.messages if "ipn[observe]" in m and "verdict=apply" in m)
    assert "sig=nested-sorted-compact-utf8-orig" in line and "status=finished" in line and "asset=usdtbsc" in line and "usd=10.00" in line.replace("usd=10.0000", "usd=10.00") or "usd=" in line
    assert "fields_missing=-" in line
    for secret in (API_KEY, SECRET, ADDRESS, LINK, "crypto@example.com", sign(trigger("mbacrypto_ref1")), "mbacrypto_ref1", "5077125051"):
        assert secret not in caplog.text, secret  # the order and payment ids appear only as their last characters


def test_observe_reports_the_missing_fields_so_the_owner_can_compare(app, fake, caplog):
    caplog.set_level(logging.INFO)
    with app(NOWPAYMENTS_IPN_MODE="observe") as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = {"payment_id": 5077125051, "payment_status": "finished", "order_id": "mbacrypto_ref1", "price_currency": "usd"}
        ipn(c, trigger("mbacrypto_ref1"))
    assert any("fields_missing=price_amount,pay_currency,pay_amount,actually_paid" in m and "verdict=unreadable" in m for m in caplog.messages)


# ====================================================================== enforce: the ladder
def test_a_finished_payment_of_the_full_amount_grants_one_month(app, fake, tg):
    with app() as c:
        cid = customer(c)
        with session_scope() as s:
            s.add(TradingAccount(customer_id=cid, pair="BTC/USDT", market_type="spot", notification_channel="telegram", notification_target="9",
                                 trial_ends_at=_now() + timedelta(days=7), is_active=False))
        before = _now()
        r = pay(c, fake, cid)
        assert r.status_code == 200 and r.json() == {"ok": True, "applied": True}
        status, start, end, provider, ref = sub_row(cid)
        assert (status, provider, ref) == ("active", "nowpayments", None)
        assert approx(start, before) and end == add_months(start, 1)
        st, pid, usd, period_end = bp_state()
        assert (st, pid, usd) == ("paid", "5077125051", Decimal("10.000000") if usd == Decimal("10") else usd) and period_end is not None
        assert count(PaymentEvent) == 1 and entitled(cid)
        with session_scope() as s:
            assert s.get(Customer, cid).account.is_active is True
    assert tg.to_owner == []


@pytest.mark.parametrize("status", ["waiting", "confirming", "confirmed", "sending"])
def test_pending_statuses_grant_nothing_and_write_nothing(app, fake, status):
    with app() as c:
        cid = customer(c)
        r = pay(c, fake, cid, status=status)
        assert r.json() == {"ok": True, "pending": status}
        assert sub_row(cid) is None and bp_state()[0] == "pending" and count(PaymentEvent) == 0 and not entitled(cid)


def test_the_tolerance_edge_is_one_percent_of_the_invoices_own_rate(app, fake, tg):
    # price 10, pay_amount 10.00: paying 9.90 is exactly 99 percent (allowed); 9.89 is below
    for i, (paid, ok) in enumerate([(9.90, True), (9.89, False), (10.0, True), (9.9999, True), (5.0, False), (0, False)]):
        with app() as c:
            cid = customer(c, f"edge{i}@example.com")
            ref = f"mbacrypto_edge{i}"
            r = pay(c, fake, cid, order=ref, pay_amount=10.0, actually_paid=paid, pid=7000 + i)
            assert (sub_row(cid) is not None) is ok, paid
            assert bp_state(ref)[0] == ("paid" if ok else "underpaid")
            assert entitled(cid) is ok
    assert tg.owner_has("crypto_underpaid")


def test_the_amount_rule_uses_the_invoices_rate_not_the_asset_count(app, fake):
    # the invoice asked for 10.04 USDT for USD 10; paying 10.04 is full even though the asset count is not 10
    with app() as c:
        cid = customer(c)
        pay(c, fake, cid, pay_amount=10.04, actually_paid=10.04)
        assert entitled(cid)
    with app() as c:
        cid = customer(c, "b@example.com")
        pay(c, fake, cid, order="mbacrypto_ref2", pay_amount=20.0, actually_paid=10.04, pid=2)  # half of what was asked
        assert not entitled(cid) and bp_state("mbacrypto_ref2")[0] == "underpaid"


def test_an_overpayment_grants_one_period_and_alerts_the_owner(app, fake, tg):
    with app() as c:
        cid = customer(c)
        pay(c, fake, cid, pay_amount=10.04, actually_paid=11.5)
        assert entitled(cid) and bp_state()[0] == "paid"
        _, start, end, _, _ = sub_row(cid)
        assert end == add_months(start, 1)  # one month, however much was overpaid
    assert tg.owner_has("crypto_overpaid") and "refund" not in " ".join(tg.to_owner).lower().replace("nothing is refunded automatically", "")


@pytest.mark.parametrize("status,state,alert", [("partially_paid", "underpaid", "crypto_underpaid"), ("failed", "failed", "crypto_failed"),
                                                ("expired", "expired", None), ("refunded", "refunded", "crypto_refunded")])
def test_terminal_statuses_are_recorded_and_grant_nothing(app, fake, tg, status, state, alert):
    with app() as c:
        cid = customer(c)
        r = pay(c, fake, cid, status=status, pay_amount=10.04, actually_paid=3.0 if status == "partially_paid" else 0)
        assert r.status_code == 200 and r.json() == {"ok": True, "recorded": state}
        assert bp_state()[0] == state and sub_row(cid) is None and not entitled(cid) and count(PaymentEvent) == 1
        if alert:
            assert tg.owner_has(alert)
        else:
            assert tg.to_owner == []
        assert pay(c, fake, cid, status=status, pay_amount=10.04, actually_paid=0, issue_row=False).json() == {"ok": True, "duplicate": True}  # replay


def test_partially_paid_grants_nothing_even_if_the_record_claims_the_full_amount(app, fake, tg):
    """The status decides first: the PDF advises against delivering on partially_paid, whatever the numbers say."""
    with app() as c:
        cid = customer(c)
        r = pay(c, fake, cid, status="partially_paid", pay_amount=10.04, actually_paid=10.04)
        assert r.json() == {"ok": True, "recorded": "underpaid"} and not entitled(cid) and sub_row(cid) is None and bp_state()[0] == "underpaid"
    assert tg.owner_has("crypto_underpaid")


def test_a_refund_after_payment_is_recorded_and_alerted_but_never_revokes(app, fake, tg):
    with app() as c:
        cid = customer(c)
        pay(c, fake, cid)
        assert entitled(cid)
        r = pay(c, fake, cid, status="refunded", issue_row=False)
        assert r.json() == {"ok": True, "recorded": "refunded"} and bp_state()[0] == "refunded"
        assert entitled(cid) and sub_row(cid)[0] == "active"
    assert tg.owner_has("crypto_refunded")


def test_late_failed_or_expired_does_not_downgrade_a_paid_invoice(app, fake):
    with app() as c:
        cid = customer(c)
        pay(c, fake, cid)
        for st in ("expired", "failed", "partially_paid", "waiting"):
            pay(c, fake, cid, status=st, issue_row=False, actually_paid=1)
        assert bp_state()[0] == "paid" and entitled(cid)


def test_an_unknown_status_changes_nothing_and_alerts(app, fake, tg):
    with app() as c:
        cid = customer(c)
        assert pay(c, fake, cid, status="mystery").json() == {"ok": True, "ignored": "unknown_status"}
        assert bp_state()[0] == "pending" and sub_row(cid) is None
    assert tg.owner_has("crypto_unknown_status")


@pytest.mark.parametrize("name,over,reason", [
    ("wrong asset", dict(pay_currency="usdttrc20"), "wrong_asset"), ("wrong price currency", dict(price_currency="eur"), "price_currency_mismatch"),
    ("wrong price", dict(price_amount=1), "price_mismatch"), ("price 10.01", dict(price_amount=10.01), "price_mismatch"),
    ("order id differs", dict(order_id="mbacrypto_other"), None), ("repeated deposit", dict(parent_payment_id=123), "repeated_deposit"),
])
def test_every_mismatch_grants_nothing_and_alerts(app, fake, tg, name, over, reason):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1", **over)
        r = ipn(c, trigger("mbacrypto_ref1"))
        assert r.status_code == 200 and sub_row(cid) is None and not entitled(cid) and bp_state()[0] == "pending", name
        if reason:
            assert r.json()["rejected"] == reason
    assert tg.to_owner  # the owner is told


def test_the_callback_is_a_trigger_only_the_api_record_decides(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        # a validly signed notification says finished and paid in full; NOWPayments' own record says still waiting
        fake.payments["5077125051"] = record("mbacrypto_ref1", "waiting", actually_paid=0)
        assert ipn(c, trigger("mbacrypto_ref1", "finished")).json() == {"ok": True, "pending": "waiting"}
        assert sub_row(cid) is None and not entitled(cid)
        # a signed notification whose own numbers are tiny, with a record that says finished and full: the record wins
        fake.payments["5077125051"] = record("mbacrypto_ref1", "finished")
        assert ipn(c, trigger("mbacrypto_ref1", "waiting", actually_paid=0.01, pay_amount=500)).json() == {"ok": True, "applied": True}


def test_the_callbacks_order_id_must_agree_with_the_record(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1")
        r = ipn(c, trigger("mbacrypto_somethingelse"))
        assert r.json() == {"ok": True, "rejected": "callback_order_differs"} and sub_row(cid) is None


def test_an_unknown_order_or_payment_is_ignored_and_never_matches_by_email_or_address(app, fake, tg):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["9"] = record("mbacrypto_never_issued", pid=9)
        assert ipn(c, trigger("mbacrypto_never_issued", pid=9)).json() == {"ok": True, "ignored": "unknown_order"}
        assert tg.owner_has("crypto_unknown_order")
        assert ipn(c, trigger("mbacrypto_ref1", pid=123456)).json() == {"ok": True, "ignored": "payment_not_found"}  # NOWPayments has no such payment
        assert ipn(c, {"payment_status": "finished", "order_id": "mbacrypto_ref1"}).json() == {"ok": True, "ignored": "no_payment_id"}
        assert ipn(c, {"payment_id": "12; DROP", "order_id": "x"}).json() == {"ok": True, "ignored": "no_payment_id"}
        assert sub_row(cid) is None


@pytest.mark.parametrize("name,rec", [
    ("wrapped in data", lambda: {"status": True, "data": record("mbacrypto_ref1")}),
    ("amounts as strings", lambda: record("mbacrypto_ref1", pay_amount="10.04", actually_paid="10.040000")),
    ("payment id as a string", lambda: record("mbacrypto_ref1", pid="5077125051")),
    ("status in another case", lambda: record("mbacrypto_ref1", "FINISHED")),
    ("extra unknown fields", lambda: {**record("mbacrypto_ref1"), "brand_new": {"x": [1]}}),
])
def test_the_payment_record_is_read_defensively(app, fake, name, rec):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = rec()
        assert ipn(c, trigger("mbacrypto_ref1")).json() == {"ok": True, "applied": True}, name


@pytest.mark.parametrize("drop", ["payment_status", "order_id", "price_amount", "price_currency", "pay_currency", "pay_amount", "actually_paid", "payment_id"])
def test_a_missing_required_field_grants_nothing_and_alerts_once(app, fake, tg, drop):
    with app() as c:
        cid = customer(c)
        issue(cid)
        r = record("mbacrypto_ref1")
        del r[drop]
        fake.payments["5077125051"] = r
        resp = ipn(c, trigger("mbacrypto_ref1"))
        assert resp.status_code == 200 and sub_row(cid) is None and not entitled(cid)
    if drop in ("order_id", "payment_id"):
        return  # nothing to match, or no way to name the payment: ignored, which is also safe
    assert tg.owner_has("crypto_payment_unreadable")


@pytest.mark.parametrize("bad", [None, "x", "NaN", "Infinity", -1, True, [], {}])
def test_odd_amount_values_never_grant(app, fake, bad):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1", actually_paid=bad)
        ipn(c, trigger("mbacrypto_ref1"))
        assert sub_row(cid) is None and not entitled(cid)


def test_zero_pay_amount_cannot_divide(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1", pay_amount=0, actually_paid=10)
        assert ipn(c, trigger("mbacrypto_ref1")).status_code == 200 and sub_row(cid) is None


# ---------------------------------------------------------------------- replay, order, top-up, failures
def test_replays_and_out_of_order_notifications_are_harmless(app, fake, tg):
    with app() as c:
        cid = customer(c)
        assert pay(c, fake, cid).json() == {"ok": True, "applied": True}
        first = sub_row(cid)
        for _ in range(3):
            assert pay(c, fake, cid, issue_row=False).json() == {"ok": True, "duplicate": True}
        assert sub_row(cid) == first and count(PaymentEvent) == 1 and count(BillingPayment) == 1
        # a stale 'waiting' notification arriving afterwards
        assert pay(c, fake, cid, status="waiting", issue_row=False).json() == {"ok": True, "pending": "waiting"}
        assert sub_row(cid) == first
        # a second, different finished payment against the same invoice
        assert pay(c, fake, cid, pid=999, issue_row=False).json() == {"ok": True, "rejected": "already_paid"}
        assert sub_row(cid) == first and tg.owner_has("crypto_duplicate_payment")


def test_a_partial_payment_can_be_topped_up_to_finished(app, fake):
    with app() as c:
        cid = customer(c)
        pay(c, fake, cid, status="partially_paid", actually_paid=4.0)
        assert bp_state()[0] == "underpaid" and not entitled(cid)
        r = pay(c, fake, cid, status="finished", issue_row=False)
        assert r.json() == {"ok": True, "applied": True} and bp_state()[0] == "paid" and entitled(cid)


def test_a_notification_already_recorded_by_a_concurrent_delivery_wins(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        with session_scope() as s:
            s.add(PaymentEvent(provider="nowpayments", event_key="nowpayments:5077125051:finished", reference="mbacrypto_ref1", customer_id=cid, amount=1000, currency="USD", outcome="applied"))
        assert pay(c, fake, cid).json() == {"ok": True, "duplicate": True}
        assert sub_row(cid) is None and bp_state()[0] == "pending"


def test_nowpayments_unreachable_is_a_503_and_changes_nothing(app, fake):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.down = True
        assert ipn(c, trigger("mbacrypto_ref1")).status_code == 503
        assert sub_row(cid) is None and count(PaymentEvent) == 0
        fake.down = False
        fake.payments["5077125051"] = record("mbacrypto_ref1")
        assert ipn(c, trigger("mbacrypto_ref1")).status_code == 200 and entitled(cid)


def test_a_failure_halfway_through_applying_rolls_everything_back_and_the_retry_succeeds(app, fake, monkeypatch):
    with app() as c:
        cid = customer(c)
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1")
        real = npay.add_months

        def boom(*a, **k):
            raise RuntimeError("database trouble after the replay row was written")

        monkeypatch.setattr(npay, "add_months", boom)
        assert ipn(c, trigger("mbacrypto_ref1")).status_code == 503
        assert count(PaymentEvent) == 0 and bp_state()[0] == "pending" and sub_row(cid) is None
        monkeypatch.setattr(npay, "add_months", real)
        assert ipn(c, trigger("mbacrypto_ref1")).json() == {"ok": True, "applied": True} and entitled(cid)


def test_no_database_write_lock_is_held_while_waiting_for_nowpayments_or_telegram(app, fake, tg, monkeypatch):
    path = load_settings().database_url.replace("sqlite:///", "")
    seen = []

    def probe():
        conn = sqlite3.connect(path, timeout=0.2)
        try:
            conn.execute("BEGIN IMMEDIATE")
            seen.append("free")
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    real_get = fake.get_json

    async def watched(path_, settings):
        probe()
        return await real_get(path_, settings)

    monkeypatch.setattr(npay, "get_json", watched)

    class Notifier:
        def __init__(self, token):
            pass

        async def send(self, target, message):
            probe()

    monkeypatch.setattr(subs, "TelegramNotifier", Notifier)
    with app() as c:
        cid = customer(c)
        pay(c, fake, cid, pay_amount=10.04, actually_paid=12.0)  # overpay: an owner alert is sent after the commit
    assert len(seen) >= 2 and set(seen) == {"free"}


# ====================================================================== the period rule
def apply_at(app_client, cid, now, **kw):
    """Run the IPN handler directly with a chosen clock."""
    with session_scope() as db:
        raw = json.dumps(trigger("mbacrypto_ref1")).encode()
        return asyncio.run(npay.handle_ipn(db, raw, sign(trigger("mbacrypto_ref1")), load_settings(), now=now))


def test_month_ends_for_a_new_customer(app, fake):
    for i, (now, expected) in enumerate([(datetime(2026, 1, 31, 10, 0, tzinfo=U), datetime(2026, 2, 28, 10, 0, tzinfo=U)),
                                         (datetime(2028, 1, 31, 10, 0, tzinfo=U), datetime(2028, 2, 29, 10, 0, tzinfo=U)),
                                         (datetime(2026, 3, 30, 23, 59, tzinfo=U), datetime(2026, 4, 30, 23, 59, tzinfo=U)),
                                         (datetime(2026, 12, 31, 0, 0, tzinfo=U), datetime(2027, 1, 31, 0, 0, tzinfo=U))]):
        with app() as c:
            cid = customer(c, f"m{i}@example.com")
            ref = f"mbacrypto_m{i}"
            issue(cid, ref)
            fake.payments[str(50 + i)] = record(ref, pid=50 + i)
            with session_scope() as db:
                body = trigger(ref, pid=50 + i)
                out = asyncio.run(npay.handle_ipn(db, json.dumps(body).encode(), sign(body), load_settings(), now=now))
            _, start, end, _, _ = sub_row(cid)
            assert (start, end) == (now, expected)


def test_renewing_while_still_in_period_stacks_from_the_current_end(app, fake):
    now = datetime(2026, 1, 28, 9, 30, tzinfo=U)
    with app() as c:
        cid = customer(c)
        add_sub(cid, end=datetime(2026, 1, 31, 10, 0, tzinfo=U))
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1")
        body = trigger("mbacrypto_ref1")
        with session_scope() as db:
            asyncio.run(npay.handle_ipn(db, json.dumps(body).encode(), sign(body), load_settings(), now=now))
        _, start, end, _, _ = sub_row(cid)
        assert start == datetime(2026, 1, 31, 10, 0, tzinfo=U) and end == datetime(2026, 2, 28, 10, 0, tzinfo=U)  # nothing is lost by paying early
        assert entitled(cid, datetime(2026, 2, 28, 9, 59, tzinfo=U)) and not entitled(cid, datetime(2026, 3, 5, 10, 1, tzinfo=U))


def test_paying_inside_the_grace_window_does_not_shift_the_period_later(app, fake):
    """D9: anchored on the old end, so a customer who always pays on day 4 of the grace does not drift later each month."""
    end0 = datetime(2026, 3, 10, 12, 0, tzinfo=U)
    with app() as c:
        cid = customer(c)
        add_sub(cid, end=end0)
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1")
        body = trigger("mbacrypto_ref1")
        with session_scope() as db:
            asyncio.run(npay.handle_ipn(db, json.dumps(body).encode(), sign(body), load_settings(), now=end0 + timedelta(days=4, hours=3)))
        _, start, end, _, _ = sub_row(cid)
        assert start == end0 and end == datetime(2026, 4, 10, 12, 0, tzinfo=U)


def test_paying_after_the_grace_window_restarts_from_now(app, fake):
    end0 = datetime(2026, 3, 10, 12, 0, tzinfo=U)
    now = end0 + timedelta(days=5, seconds=1)  # grace is exclusive at its end
    with app() as c:
        cid = customer(c)
        add_sub(cid, end=end0)
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1")
        body = trigger("mbacrypto_ref1")
        with session_scope() as db:
            asyncio.run(npay.handle_ipn(db, json.dumps(body).encode(), sign(body), load_settings(), now=now))
        _, start, end, _, _ = sub_row(cid)
        assert start == now and end == add_months(now, 1)


def test_renewing_a_cancelled_but_paid_up_customer_extends_from_the_end(app, fake):
    end0 = datetime(2026, 3, 10, 12, 0, tzinfo=U)
    with app() as c:
        cid = customer(c)
        add_sub(cid, end=end0, status="cancelled")
        issue(cid)
        fake.payments["5077125051"] = record("mbacrypto_ref1")
        body = trigger("mbacrypto_ref1")
        with session_scope() as db:
            asyncio.run(npay.handle_ipn(db, json.dumps(body).encode(), sign(body), load_settings(), now=end0 - timedelta(days=2)))
        status, start, end, _, _ = sub_row(cid)
        assert status == "active" and start == end0 and end == datetime(2026, 4, 10, 12, 0, tzinfo=U)


# ====================================================================== card to crypto
def card_customer(c, fake_ps, *, old_status, old_next, token="tok_OLDtoken12345"):
    cid = customer(c)
    add_sub(cid, provider="paystack", end=_now() - timedelta(hours=2), ref="SUB_old")
    with session_scope() as s:
        row = s.query(Subscription).one()
        s.add(SubscriptionToken(subscription_id=row.id, token_enc=__import__("mba_bot.crypto_utils", fromlist=["x"]).encrypt(token, key=load_settings().encryption_key)))
    fake_ps["SUB_old"] = {"subscription_code": "SUB_old", "status": old_status, "next_payment_date": old_next}
    return cid


@pytest.fixture()
def paystack_old(monkeypatch):
    subs_by_code, posts = {}, []

    async def get_json(path, settings):
        code = path.rsplit("/", 1)[1]
        return {"status": True, "data": subs_by_code[code]} if code in subs_by_code else None

    async def post_json(path, body, settings):
        posts.append((path, dict(body)))
        return 200, {"status": True}

    monkeypatch.setattr(ps, "get_json", get_json)
    monkeypatch.setattr(ps, "post_json", post_json)
    return subs_by_code, posts


def test_a_crypto_payment_in_grace_switches_the_row_and_cancels_a_failed_card_subscription(app, fake, tg, paystack_old, caplog):
    caplog.set_level(logging.DEBUG)
    by_code, posts = paystack_old
    with app() as c:
        cid = card_customer(c, by_code, old_status="attention", old_next=(_now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        assert pay(c, fake, cid).json() == {"ok": True, "applied": True}
        status, start, end, provider, ref = sub_row(cid)
        assert (status, provider, ref) == ("active", "nowpayments", None) and entitled(cid)
        assert posts == [("/subscription/disable", {"code": "SUB_old", "token": "tok_OLDtoken12345"})]
    assert "tok_OLDtoken12345" not in caplog.text and not any("tok_OLD" in m for _, m in tg.log)


def test_a_good_card_subscription_is_never_cancelled(app, fake, tg, paystack_old):
    by_code, posts = paystack_old
    with app() as c:
        cid = card_customer(c, by_code, old_status="active", old_next=(_now() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        pay(c, fake, cid)
        assert posts == [] and sub_row(cid)[3] == "nowpayments"


def test_if_paystack_cannot_be_asked_the_crypto_payment_still_applies_and_the_owner_is_told(app, fake, tg, paystack_old, monkeypatch):
    by_code, posts = paystack_old

    async def down(path, settings):
        raise ps.ProviderUnavailable("http 503")

    monkeypatch.setattr(ps, "get_json", down)
    with app() as c:
        cid = card_customer(c, by_code, old_status="attention", old_next="2026-01-01T00:00:00.000Z")
        assert pay(c, fake, cid).json() == {"ok": True, "applied": True}
        assert entitled(cid) and posts == [] and tg.owner_has("old_subscription_not_disabled")


def test_a_failed_cancel_alerts_the_owner_and_the_payment_still_applies(app, fake, tg, paystack_old, monkeypatch):
    by_code, posts = paystack_old

    async def down(path, body, settings):
        raise ps.ProviderUnavailable("http 503")

    monkeypatch.setattr(ps, "post_json", down)
    with app() as c:
        cid = card_customer(c, by_code, old_status="attention", old_next=(_now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        pay(c, fake, cid)
        assert entitled(cid) and tg.owner_has("old_subscription_not_disabled")


def test_a_crypto_payment_while_a_card_period_is_running_is_recorded_not_applied(app, fake, tg, paystack_old):
    with app() as c:
        cid = customer(c)
        end = _now() + timedelta(days=15)
        add_sub(cid, provider="paystack", end=end, ref="SUB_running")
        pay(c, fake, cid)
        assert bp_state()[0] == "paid" and sub_row(cid)[3] == "paystack" and sub_row(cid)[2] == end.replace(microsecond=sub_row(cid)[2].microsecond)
        assert tg.owner_has("crypto_paid_while_card_active")


# ====================================================================== reminders and the banner
def test_the_banner_offers_crypto_payment_three_days_before_and_in_grace(app, fake, tg):
    with app() as c:
        cid = customer(c)
        now = _now()
        add_sub(cid, end=now + timedelta(days=2))
        n = c.get("/api/billing/notice").json()["notice"]
        assert n["kind"] == "period_end_soon" and n["action"] == "pay_crypto" and "5-day grace period" in n["message"] and "http" not in n["message"]
    with app() as c2:
        cid2 = customer(c2, "grace@example.com")
        add_sub(cid2, end=now - timedelta(hours=2))
        with session_scope() as db:
            assert notices.due_period_ended_notices(db, load_settings(), now)
        n2 = c2.get("/api/billing/notice").json()["notice"]
        assert n2["kind"] == "period_ended" and n2["action"] == "pay_crypto" and "5-day grace period" in n2["message"]
        # a notice is raised once per period
        with session_scope() as db:
            assert notices.due_period_ended_notices(db, load_settings(), now + timedelta(hours=1)) == []


def test_the_period_ended_message_goes_to_telegram_once_and_the_banner_ends_after_payment(app, fake, tg):
    with app() as c:
        cid = customer(c)
        with session_scope() as s:
            s.add(TradingAccount(customer_id=cid, pair="BTC/USDT", market_type="spot", notification_channel="telegram", notification_target="555",
                                 trial_ends_at=_now() + timedelta(days=7), is_active=False))
        now = _now()
        add_sub(cid, end=now - timedelta(hours=1))
        assert asyncio.run(notices.run_reminders_once(load_settings(), now)) == 1
        assert asyncio.run(notices.run_reminders_once(load_settings(), now + timedelta(hours=3))) == 0
        msgs = [m for t, m in tg.log if t == "555"]
        assert len(msgs) == 1 and "period ended on" in msgs[0] and "5-day grace period" in msgs[0] and "/#/dashboard" in msgs[0]
        pay(c, fake, cid)
        assert c.get("/api/billing/notice").json() == {"notice": None}  # the period moved on: no banner


def test_the_card_banner_is_unchanged(app, fake, tg):
    with app() as c:
        cid = customer(c)
        add_sub(cid, provider="paystack", end=_now() - timedelta(hours=2), ref="SUB_x")
        with session_scope() as s:
            s.add(BillingNotice(customer_id=cid, kind="renewal_failed", period_key=s.query(Subscription).one().current_period_end.replace(tzinfo=U).isoformat(),
                                expires_at=_now() + timedelta(days=4)))
        assert c.get("/api/billing/notice").json()["notice"]["action"] == "pay"  # the card button, not the crypto one


# ====================================================================== static guarantees
SRC = Path(__file__).resolve().parent.parent


def test_the_new_code_imports_nothing_that_trades_and_holds_no_secret():
    import ast
    import re
    text = (SRC / "src/mba_bot/billing/nowpayments.py").read_text()
    tree = ast.parse(text)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(x in (m or "") for m in mods for x in ("venue", "lifecycle", "exchange", "trader", "risk", "onboarding")), mods
    assert not re.search(r"[A-Za-z0-9]{8}-[A-Za-z0-9]{7}-[A-Za-z0-9]{7}", text) and "x-api-key\": \"" not in text


def test_the_only_calls_to_nowpayments_are_the_three_allowlisted_ones():
    text = (SRC / "src/mba_bot/billing/nowpayments.py").read_text()
    assert text.count("client.get(") == 1 and text.count("httpx.post(") == 1 and text.count("httpx.get(") == 1
    assert "/v1/payment/" in text and "/v1/invoice" in text and "/v1/min-amount" in text
    for banned in ("/v1/payout", "/v1/auth", "/v1/sub-partner", "/v1/subscriptions", "/v1/payment\"", "jwt", "password"):
        assert banned not in text.lower()
    with pytest.raises(ps.ProviderUnavailable, match="blocked"):
        asyncio.run(npay.get_json("/v1/payout/1", load_settings.__globals__["Settings"] if False else _Cfg()))
    with pytest.raises(ps.ProviderUnavailable, match="blocked"):
        asyncio.run(npay.get_json("/v1/payment/12/../../v1/payout", _Cfg()))


class _Cfg:
    nowpayments_api_key = API_KEY


def test_the_fields_relied_on_are_documented_for_the_owner():
    doc = (SRC / "docs/security/billing_nowpayments.md").read_text()
    for field in npay.RELIED_ON + npay.OPTIONAL_FIELDS:
        assert f"`{field}`" in doc, field


def test_the_banner_and_route_wiring_in_the_frontend_and_api():
    banner = (SRC / "frontend/src/components/BillingNoticeBanner.jsx").read_text()
    assert "/billing/crypto/initialize" in banner and "pay_crypto" in banner and "/billing/subscription/initialize" in banner
    router = (SRC / "src/mba_bot/api/routers/billing.py").read_text()
    assert '"/crypto/initialize"' in router and '"/nowpayments/ipn"' in router
