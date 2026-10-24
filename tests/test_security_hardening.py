"""Security hardening (branch security/step1-hardening): F-01, F-03, F-04, F-05, F-06 and the prepared WAL setting.

Two kinds of test. (1) With every new switch OFF the application behaves as it did before: defaults are off, nothing
new is installed, the old webhook path runs, docs are served, no limiter, no lock. (2) With a switch ON, the new
behaviour does what it says and fails safe. No network: Paystack is replaced by a stub, the scheduler by an idle task."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from mba_bot import db as dbmod
from mba_bot.api import app as appmod
from mba_bot.api.ratelimit import LoginThrottle, SlidingWindowLimiter
from mba_bot.api.routers import billing
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.instance_lock import AlreadyRunning, acquire_single_instance_lock
from mba_bot.log_safety import secrets_from_settings
from mba_bot.models import Customer, PaymentEvent, TradingAccount

SK = "sk_test_fakefakefakefakefake"
AMOUNT = 13_500_000
FLAGS = ("PAYSTACK_STRICT_VERIFY", "API_SINGLE_INSTANCE_LOCK", "TELEGRAM_WEBHOOK_SECRET", "RATE_LIMIT_ENABLED", "LOGIN_THROTTLE_ENABLED",
         "DISABLE_API_DOCS", "SQLITE_WAL", "RATE_LIMIT_PER_MINUTE", "RATE_LIMIT_AUTH_PER_MINUTE", "LOGIN_MAX_FAILURES", "LOGIN_LOCKOUT_SECONDS")


@pytest.fixture()
def make_app(monkeypatch, tmp_path):
    def build(**env):
        for name in FLAGS:
            monkeypatch.delenv(name, raising=False)
        base = {"DATABASE_URL": f"sqlite:///{tmp_path}/t.db", "ENCRYPTION_KEY": Fernet.generate_key().decode(), "JWT_SECRET": "test-jwt-secret",
                "COOKIE_SECURE": "false", "POLL_INTERVAL_SECONDS": "3600", "PAYSTACK_SECRET_KEY": SK, "PAYSTACK_AMOUNT": str(AMOUNT),
                "PAYSTACK_CURRENCY": "NGN", "MBA_BOT_LOCK_FILE": str(tmp_path / "t.lock"), "TELEGRAM_BOT_USERNAME": "mba_test_bot"}
        base.update({k: str(v) for k, v in env.items()})
        for k, v in base.items():
            monkeypatch.setenv(k, v)

        async def idle(settings):
            await asyncio.sleep(3600)

        monkeypatch.setattr(appmod, "run_forever", idle)
        return appmod.create_app()
    return build


def client_for(app):
    return TestClient(app, base_url="https://testserver")


def sign(body: bytes) -> str:
    return hmac.new(SK.encode(), body, hashlib.sha512).hexdigest()


def post_event(client, event: dict, *, signature: str | None = None):
    body = json.dumps(event).encode()
    return client.post("/api/billing/webhook", content=body, headers={"x-paystack-signature": signature if signature is not None else sign(body)})


def new_customer(email="a@example.com", *, reference="ref_1", paid=False) -> str:
    with session_scope() as s:
        c = Customer(email=email, password_hash="x", has_paid=paid, paystack_reference=reference)
        s.add(c)
        s.flush()
        s.add(TradingAccount(customer_id=c.id, pair="BTC/USDT", market_type="spot", notification_channel="telegram",
                             trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7), is_active=False))
        return c.id


def charge(cid, reference="ref_1"):
    return {"event": "charge.success", "data": {"status": "success", "reference": reference, "id": 99, "metadata": {"customer_id": cid}}}


def verified(cid, reference="ref_1", **over):
    data = {"status": "success", "reference": reference, "amount": AMOUNT, "currency": "NGN", "metadata": {"customer_id": cid}}
    data.update(over)
    return {"status": True, "data": data}


@pytest.fixture()
def paystack(monkeypatch):
    """Stands in for Paystack's verify endpoint. `.answer` is what it returns, `.calls` what was asked."""
    class Stub:
        answer: dict | Exception = {}
        calls: list[str] = []

    stub = Stub()
    stub.calls = []

    async def fake(reference, settings):
        stub.calls.append(reference)
        if isinstance(stub.answer, Exception):
            raise stub.answer
        return stub.answer

    monkeypatch.setattr(billing, "_verify_with_paystack", fake)
    return stub


def user(cid):
    with session_scope() as s:
        c = s.get(Customer, cid)
        return c.has_paid, c.account.is_active, c.paystack_reference


# ============================================================ every switch off = today

def test_every_new_setting_defaults_to_off_and_the_secret_is_hidden(monkeypatch):
    for name in FLAGS:
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    assert (s.paystack_strict_verify, s.api_single_instance_lock, s.rate_limit_enabled, s.login_throttle_enabled, s.disable_api_docs, s.sqlite_wal) == (False,) * 6
    assert s.telegram_webhook_secret is None
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cr3t-value-123")
    s = load_settings()
    assert "s3cr3t-value-123" not in repr(s) and "s3cr3t-value-123" in secrets_from_settings(s)


def test_with_everything_off_the_app_is_unchanged(make_app):
    app = make_app()
    assert not any(m.cls.__name__ == "RateLimitMiddleware" for m in app.user_middleware)
    assert app.docs_url == "/docs" and app.redoc_url == "/redoc" and app.openapi_url == "/openapi.json"
    with client_for(app) as c:
        assert c.get("/docs").status_code == 200 and c.get("/openapi.json").status_code == 200
        assert c.get("/api/health").json() == {"status": "ok"}
        # no limiter: many rapid logins are all answered normally
        for _ in range(30):
            assert c.post("/api/auth/login", json={"email": "x@example.com", "password": "wrong-password-1"}).status_code == 401
    assert not Path(load_settings().lock_file).exists()  # no lock taken


def test_with_the_flag_off_the_old_billing_path_runs_and_never_calls_paystack(make_app, paystack):
    with client_for(make_app()) as c:
        cid = new_customer()
        assert post_event(c, charge(cid, "whatever_ref")).status_code == 200
        assert user(cid) == (True, True, "whatever_ref")  # today's behaviour, including overwriting the reference
        assert paystack.calls == []
        with session_scope() as s:
            assert s.query(PaymentEvent).count() == 0
        assert post_event(c, charge(cid), signature="bad").status_code == 400


# ============================================================ F-01

STRICT = {"PAYSTACK_STRICT_VERIFY": "1"}


def test_a_genuine_verified_payment_unlocks_access_and_is_recorded(make_app, paystack):
    with client_for(make_app(**STRICT)) as c:
        cid = new_customer()
        paystack.answer = verified(cid)
        assert post_event(c, charge(cid)).status_code == 200
        assert user(cid) == (True, True, "ref_1") and paystack.calls == ["ref_1"]
        with session_scope() as s:
            row = s.query(PaymentEvent).one()
            assert (row.provider, row.event_key, row.reference, row.amount, row.currency, row.outcome) == ("paystack", "charge.success:ref_1", "ref_1", AMOUNT, "NGN", "applied")


def test_a_replayed_event_changes_nothing_and_does_not_ask_paystack_again(make_app, paystack):
    with client_for(make_app(**STRICT)) as c:
        cid = new_customer()
        paystack.answer = verified(cid)
        post_event(c, charge(cid))
        r = post_event(c, charge(cid))
        assert r.status_code == 200 and r.json().get("duplicate") is True
        assert paystack.calls == ["ref_1"]
        with session_scope() as s:
            assert s.query(PaymentEvent).count() == 1


def test_a_duplicate_event_row_cannot_exist_and_a_seen_event_grants_nothing(make_app, paystack):
    """The unique constraint is what decides if two deliveries race; the pre-check handles the ordinary replay."""
    with client_for(make_app(**STRICT)) as c:
        cid = new_customer()
        paystack.answer = verified(cid)
        post_event(c, charge(cid))
        with session_scope() as s:
            s.query(PaymentEvent).delete()
            s.add(PaymentEvent(provider="paystack", event_key="charge.success:ref_1", reference="ref_1", customer_id=cid, outcome="applied"))
        with session_scope() as s:
            s.get(Customer, cid).has_paid = False
        # the pre-check sees the row and returns the duplicate answer without granting anything
        assert post_event(c, charge(cid)).json().get("duplicate") is True
        assert user(cid)[0] is False
        # and the constraint itself rejects a second row for the same event
        with pytest.raises(Exception):  # noqa: B017 - IntegrityError
            with session_scope() as s:
                s.add(PaymentEvent(provider="paystack", event_key="charge.success:ref_1", outcome="applied"))


def test_a_delivery_that_loses_the_race_is_a_harmless_duplicate(make_app, paystack):
    """The other delivery commits its row while we are waiting for Paystack: our insert hits the unique constraint."""
    with client_for(make_app(**STRICT)) as c:
        cid = new_customer()

        async def slow_verify(reference, settings):
            with session_scope() as other:  # the concurrent delivery wins and records the event first
                other.add(PaymentEvent(provider="paystack", event_key=f"charge.success:{reference}", reference=reference, customer_id=cid, outcome="applied"))
            return verified(cid)

        paystack_calls = []
        orig = billing._verify_with_paystack
        billing._verify_with_paystack = slow_verify
        try:
            r = post_event(c, charge(cid))
        finally:
            billing._verify_with_paystack = orig
        assert r.status_code == 200 and r.json().get("duplicate") is True and paystack_calls == []
        with session_scope() as s:
            assert s.query(PaymentEvent).count() == 1
        assert user(cid)[0] is False  # this delivery granted nothing; the winner's own transaction does that


@pytest.mark.parametrize("name,stored_ref,answer", [
    ("reference never issued", "ref_other", None),
    ("amount too small", "ref_1", {"amount": 10_000}),
    ("amount too large is also not the price", "ref_1", {"amount": AMOUNT * 2}),
    ("wrong currency", "ref_1", {"currency": "USD"}),
    ("another customer's metadata", "ref_1", {"metadata": {"customer_id": "someone-else"}}),
    ("transaction not successful", "ref_1", {"status": "failed"}),
    ("reference differs in Paystack's record", "ref_1", {"reference": "ref_zzz"}),
    ("customer bears fees but requested amount is lower", "ref_1", {"amount": AMOUNT + 5000, "requested_amount": 10_000}),
])
def test_anything_that_does_not_match_grants_nothing(make_app, paystack, name, stored_ref, answer):
    with client_for(make_app(**STRICT)) as c:
        cid = new_customer(reference=stored_ref)
        paystack.answer = verified(cid, **(answer or {}))
        assert post_event(c, charge(cid)).status_code == 200  # final: Paystack must not retry
        assert user(cid)[0] is False, name
        with session_scope() as s:
            assert s.query(PaymentEvent).count() == 0  # rejections are not recorded, so a later genuine event still works


def test_fees_added_on_top_of_the_price_are_accepted_when_the_requested_amount_is_right(make_app, paystack):
    with client_for(make_app(**STRICT)) as c:
        cid = new_customer()
        paystack.answer = verified(cid, amount=AMOUNT + 50_000, requested_amount=AMOUNT)
        post_event(c, charge(cid))
        assert user(cid)[0] is True


def test_paystack_being_unreachable_asks_for_a_retry_and_records_nothing(make_app, paystack):
    with client_for(make_app(**STRICT)) as c:
        cid = new_customer()
        paystack.answer = billing.PaystackUnavailable("http 503")
        assert post_event(c, charge(cid)).status_code == 503
        assert user(cid)[0] is False
        with session_scope() as s:
            assert s.query(PaymentEvent).count() == 0
        paystack.answer = verified(cid)  # Paystack's retry succeeds
        assert post_event(c, charge(cid)).status_code == 200 and user(cid)[0] is True


def test_nothing_is_granted_if_the_price_is_not_configured(make_app, paystack):
    with client_for(make_app(**STRICT, PAYSTACK_AMOUNT=0)) as c:
        cid = new_customer()
        paystack.answer = verified(cid, amount=0)
        post_event(c, charge(cid))
        assert user(cid)[0] is False


def test_the_signature_is_still_checked_first_and_other_events_are_ignored(make_app, paystack):
    with client_for(make_app(**STRICT)) as c:
        cid = new_customer()
        assert post_event(c, charge(cid), signature="nope").status_code == 400 and paystack.calls == []
        assert post_event(c, {"event": "charge.failed", "data": {"reference": "ref_1", "metadata": {"customer_id": cid}}}).status_code == 200
        assert post_event(c, {"event": "charge.success", "data": {"status": "success", "metadata": {}}}).status_code == 200
        assert paystack.calls == [] and user(cid)[0] is False


def test_existing_lifetime_customers_keep_exactly_their_access(make_app, paystack):
    with client_for(make_app(**STRICT)) as c:
        grandfathered = new_customer("old@example.com", reference=None, paid=True)  # approved by hand, never paid through Paystack
        with session_scope() as s:
            s.get(Customer, grandfathered).account.is_active = True
        # a stray or repeated event for them changes nothing
        paystack.answer = verified(grandfathered)
        post_event(c, charge(grandfathered))
        assert user(grandfathered) == (True, True, None)
        # a paid customer who gets a valid event for their own reference: recorded as already paid, state unchanged
        paid = new_customer("paid@example.com", reference="ref_p", paid=True)
        paystack.answer = verified(paid, "ref_p")
        post_event(c, charge(paid, "ref_p"))
        assert user(paid)[0] is True
        with session_scope() as s:
            assert s.query(PaymentEvent).filter_by(customer_id=paid).one().outcome == "already_paid"


def test_the_payment_events_table_is_additive_and_holds_no_secrets():
    cols = {c.name for c in PaymentEvent.__table__.columns}
    assert cols == {"id", "provider", "event_key", "reference", "customer_id", "amount", "currency", "outcome", "created_at"}
    assert any(c.name == "uq_payment_events_provider_event_key" for c in PaymentEvent.__table__.constraints)


@pytest.mark.parametrize("status_code,body,expect", [(200, {"status": True, "data": {"status": "success"}}, "ok"), (404, {}, "not_found"),
                                                      (500, {}, "down"), (429, {}, "down"), (200, "not json", "down")])
def test_the_verify_call_asks_the_right_url_with_the_key_and_fails_safe(monkeypatch, status_code, body, expect):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        return httpx.Response(status_code, json=body) if not isinstance(body, str) else httpx.Response(status_code, text=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(billing.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    from dataclasses import replace
    settings = replace(load_settings(), paystack_secret_key=SK)

    async def go():
        return await billing._verify_with_paystack("ref/with space&x", settings)

    if expect == "down":
        with pytest.raises(billing.PaystackUnavailable) as e:
            asyncio.run(go())
        assert SK not in str(e.value)
    else:
        out = asyncio.run(go())
        assert out.get("status") is (True if expect == "ok" else False)
    assert seen["url"].endswith("/transaction/verify/ref%2Fwith%20space%26x") and seen["auth"] == f"Bearer {SK}"


def test_a_network_error_is_reported_without_leaking_the_key(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom " + SK)

    real = httpx.AsyncClient
    monkeypatch.setattr(billing.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    from dataclasses import replace
    with pytest.raises(billing.PaystackUnavailable) as e:
        asyncio.run(billing._verify_with_paystack("r", replace(load_settings(), paystack_secret_key=SK)))
    assert SK not in str(e.value) and "ConnectError" in str(e.value)


def test_the_proposed_migration_is_idempotent_and_reversible(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    spec = importlib.util.spec_from_file_location("proposed", Path(__file__).parent.parent / "docs" / "security" / "proposed_migration_payment_events.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.down_revision == "c7d2e5f8a1b3"
    import re

    live = {re.search(r'^revision[^=\n]*=\s*["\'](\w+)["\']', p.read_text(), re.M).group(1) for p in (Path(__file__).parent.parent / "alembic" / "versions").glob("*.py")}
    assert mod.revision not in live and "c7d2e5f8a1b3" in live  # proposed, not installed; chains onto the current head
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    with eng.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
            assert inspect(conn).has_table("payment_events")
            mod.upgrade()  # already there (for example created by init_db): no error
            mod.downgrade()
            assert not inspect(conn).has_table("payment_events")
            mod.downgrade()


# ============================================================ F-03

def test_flag_on_takes_the_lock_while_running_and_releases_it_after(make_app, tmp_path):
    app = make_app(API_SINGLE_INSTANCE_LOCK="1")
    lock = str(tmp_path / "t.lock")
    with client_for(app):
        with pytest.raises(AlreadyRunning):
            acquire_single_instance_lock(lock)  # a second worker or copy cannot get it
    h = acquire_single_instance_lock(lock)  # released at shutdown
    h.close()


def test_flag_on_refuses_to_start_a_second_copy(make_app, tmp_path):
    held = acquire_single_instance_lock(str(tmp_path / "t.lock"))
    try:
        with pytest.raises(RuntimeError, match="already|running|lock"):
            with client_for(make_app(API_SINGLE_INSTANCE_LOCK="1")):
                pass
    finally:
        held.close()


def test_flag_off_ignores_the_lock_entirely(make_app, tmp_path):
    held = acquire_single_instance_lock(str(tmp_path / "t.lock"))
    try:
        with client_for(make_app()) as c:  # starts normally even though the lock is held, exactly as today
            assert c.get("/api/health").status_code == 200
    finally:
        held.close()


# ============================================================ F-05

def test_telegram_webhook_without_a_configured_secret_accepts_everything_as_before(make_app):
    with client_for(make_app()) as c:
        msg = {"message": {"chat": {"id": 1}, "text": "hello"}}
        assert c.post("/api/telegram/webhook", json=msg).status_code == 200
        assert c.post("/api/telegram/webhook", json=msg, headers={"X-Telegram-Bot-Api-Secret-Token": "anything"}).status_code == 200


def test_telegram_webhook_with_a_secret_requires_the_exact_header(make_app):
    with client_for(make_app(TELEGRAM_WEBHOOK_SECRET="correct-secret_123")) as c:
        msg = {"message": {"chat": {"id": 1}, "text": "hello"}}
        assert c.post("/api/telegram/webhook", json=msg).status_code == 403
        assert c.post("/api/telegram/webhook", json=msg, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
        assert c.post("/api/telegram/webhook", json=msg, headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret_123 "}).status_code == 403
        assert c.post("/api/telegram/webhook", json=msg, headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret_123"}).status_code == 200


def test_the_header_comparison_is_constant_time():
    src = (Path(__file__).parent.parent / "src" / "mba_bot" / "api" / "routers" / "telegram.py").read_text()
    assert "hmac.compare_digest" in src and "secret-token" in src


# ============================================================ F-04

def test_sliding_window_blocks_after_the_limit_and_recovers():
    t = [0.0]
    lim = SlidingWindowLimiter(clock=lambda: t[0])
    assert [lim.hit("k", 3)[0] for _ in range(3)] == [True, True, True]
    ok, retry = lim.hit("k", 3)
    assert ok is False and 1 <= retry <= 61
    assert lim.hit("other", 3)[0] is True  # keys are independent
    t[0] = 61.0
    assert lim.hit("k", 3)[0] is True


def test_limiter_memory_is_bounded():
    t = [0.0]
    lim = SlidingWindowLimiter(clock=lambda: t[0])
    for i in range(25_000):
        t[0] += 0.0001
        lim.hit(f"ip{i}", 5)
    assert len(lim._hits) <= 20_001


def test_login_throttle_counts_failures_per_address_and_email_and_expires():
    t = [0.0]
    th = LoginThrottle(max_failures=3, lockout_seconds=900, clock=lambda: t[0])
    for _ in range(3):
        assert th.blocked("1.1.1.1", "A@x.com") == 0
        th.record_failure("1.1.1.1", "a@x.com")
    assert th.blocked("1.1.1.1", "a@x.com") > 0
    assert th.blocked("2.2.2.2", "a@x.com") == 0  # another address is not locked out of that account
    assert th.blocked("1.1.1.1", "b@x.com") == 0   # and this address can still try another account (up to its own cap)
    t[0] = 901
    assert th.blocked("1.1.1.1", "a@x.com") == 0
    th2 = LoginThrottle(max_failures=2, lockout_seconds=900, clock=lambda: t[0])
    th2.record_failure("9.9.9.9", "a@x.com")
    th2.record_success("9.9.9.9", "a@x.com")
    th2.record_failure("9.9.9.9", "a@x.com")
    assert th2.blocked("9.9.9.9", "a@x.com") == 0  # a success cleared the count
    for i in range(8):
        th2.record_failure("9.9.9.9", f"user{i}@x.com")
    assert th2.blocked("9.9.9.9", "fresh@x.com") > 0  # one address spraying many accounts is stopped by the address cap


def test_the_app_limiter_blocks_with_429_and_retry_after_and_keeps_cors_headers(make_app):
    with client_for(make_app(RATE_LIMIT_ENABLED="1", RATE_LIMIT_AUTH_PER_MINUTE="3", RATE_LIMIT_PER_MINUTE="1000")) as c:
        h = {"Origin": "http://localhost:5173"}
        codes = [c.post("/api/auth/login", json={"email": "x@example.com", "password": "wrong-password-1"}, headers=h).status_code for _ in range(5)]
        assert codes == [401, 401, 401, 429, 429]
        r = c.post("/api/auth/login", json={"email": "x@example.com", "password": "wrong-password-1"}, headers=h)
        assert int(r.headers["retry-after"]) >= 1 and r.headers.get("access-control-allow-origin") == "http://localhost:5173"
        assert c.get("/api/health").status_code == 200  # other routes use the general limit


def test_provider_callbacks_and_health_are_never_limited(make_app):
    with client_for(make_app(RATE_LIMIT_ENABLED="1", RATE_LIMIT_PER_MINUTE="2", RATE_LIMIT_AUTH_PER_MINUTE="2")) as c:
        for _ in range(20):
            assert c.get("/api/health").status_code == 200
            assert c.post("/api/telegram/webhook", json={}).status_code == 200
            assert c.post("/api/billing/webhook", content=b"{}", headers={"x-paystack-signature": "bad"}).status_code == 400  # reached the handler, not the limiter


def test_general_limit_applies_to_other_api_routes(make_app):
    with client_for(make_app(RATE_LIMIT_ENABLED="1", RATE_LIMIT_PER_MINUTE="4")) as c:
        codes = [c.get("/api/auth/me").status_code for _ in range(6)]
        assert codes == [401, 401, 401, 401, 429, 429]


def test_login_throttle_locks_after_repeated_failures_even_for_the_right_password(make_app):
    with client_for(make_app(LOGIN_THROTTLE_ENABLED="1", LOGIN_MAX_FAILURES="3")) as c:
        c.post("/api/auth/signup", json={"email": "real@example.com", "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True})
        c.post("/api/auth/logout")
        bad = {"email": "real@example.com", "password": "wrong-password-1"}
        assert [c.post("/api/auth/login", json=bad).status_code for _ in range(3)] == [401, 401, 401]
        locked = c.post("/api/auth/login", json={"email": "real@example.com", "password": "correct-horse-battery"})
        assert locked.status_code == 429 and "retry-after" in locked.headers
        # an unknown email behaves the same way, so the throttle reveals nothing about which accounts exist
        ghost = {"email": "ghost@example.com", "password": "wrong-password-1"}
        assert [c.post("/api/auth/login", json=ghost).status_code for _ in range(4)] == [401, 401, 401, 429]


def test_a_successful_login_resets_the_count(make_app):
    with client_for(make_app(LOGIN_THROTTLE_ENABLED="1", LOGIN_MAX_FAILURES="3")) as c:
        c.post("/api/auth/signup", json={"email": "ok@example.com", "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True})
        bad = {"email": "ok@example.com", "password": "wrong-password-1"}
        good = {"email": "ok@example.com", "password": "correct-horse-battery"}
        for _ in range(2):
            c.post("/api/auth/login", json=bad)
        assert c.post("/api/auth/login", json=good).status_code == 200
        for _ in range(2):
            c.post("/api/auth/login", json=bad)
        assert c.post("/api/auth/login", json=good).status_code == 200


# ============================================================ F-06

@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_are_not_served_when_disabled(make_app, path):
    with client_for(make_app(DISABLE_API_DOCS="1")) as c:
        assert c.get(path).status_code == 404
        assert c.get("/api/health").status_code == 200


# ============================================================ WAL (prepared, not deployed)

def _mode(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()


def test_default_init_db_leaves_the_journal_mode_alone(tmp_path):
    init_db(f"sqlite:///{tmp_path}/d.db")
    with dbmod._engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "delete"


def test_wal_mode_sets_wal_busy_timeout_and_normal_sync_on_every_connection(tmp_path):
    init_db(f"sqlite:///{tmp_path}/w.db", wal=True, busy_timeout_ms=12345)
    for _ in range(2):
        with dbmod._engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 12345
            assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1


def test_the_flag_alone_does_not_undo_wal_the_rollback_does(tmp_path):
    p = tmp_path / "r.db"
    init_db(f"sqlite:///{p}", wal=True)
    with session_scope() as s:
        s.add(Customer(email="k@example.com", password_hash="x"))
    assert _mode(str(p)) == "wal"
    dbmod._engine.dispose()
    init_db(f"sqlite:///{p}", wal=False)  # flag turned off: the file is still WAL (it is stored in the file)
    assert _mode(str(p)) == "wal"
    dbmod._engine.dispose()
    con = sqlite3.connect(p)  # the runbook's rollback, run with the service stopped
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert con.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    assert con.execute("SELECT count(*) FROM customers").fetchone()[0] == 1
    con.close()
    assert _mode(str(p)) == "delete" and not Path(str(p) + "-wal").exists()


def test_the_online_backup_of_a_wal_database_is_complete_but_a_plain_copy_is_not(tmp_path):
    p = tmp_path / "b.db"
    con = sqlite3.connect(p)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
    con.commit()
    plain = tmp_path / "plain.db"
    plain.write_bytes(p.read_bytes())  # what a file-copy backup does: misses what is still in the -wal file
    good = tmp_path / "good.db"
    dest = sqlite3.connect(good)
    con.backup(dest)
    dest.close()
    assert sqlite3.connect(good).execute("SELECT count(*) FROM t").fetchone()[0] == 50
    assert sqlite3.connect(good).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    try:
        n_plain = sqlite3.connect(plain).execute("SELECT count(*) FROM t").fetchone()[0]
    except sqlite3.OperationalError:
        n_plain = 0
    assert n_plain < 50
    con.close()
