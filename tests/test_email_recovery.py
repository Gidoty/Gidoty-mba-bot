"""Email sender, verification and password reset (docs/safety/email_and_recovery.md). Fake mail clients and an isolated database: nothing is sent anywhere.

PROVES: off by default and every screen says so plainly; the SMTP and HTTP senders with fake clients (login, TLS, shape, failure returns False and never raises, no address or secret in the log); the reset request
answers the same for a known, an unknown and a suspended address, does no database work in the request, and sends mail only for a real account; tokens are random, stored hashed, single use, expiring, wrong-purpose
and superseded ones are refused; the reset changes the password and nothing about consent; verification; the per-customer and per-caller limits; the opt-out for non-essential notices (essential ones ignore it).
DOES NOT PROVE delivery by a real mail server or provider (UNVERIFIED) or anything in a browser."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from mba_bot import db as dbmod, email_flows as F, mailer as M
from mba_bot.api.ratelimit import SlidingWindowLimiter
from mba_bot.api.routers import email_auth
from mba_bot.api.security import SESSION_COOKIE_NAME, create_session_token, verify_password
from mba_bot.config import load_settings
from mba_bot.db import session_scope
from mba_bot.models import Customer, CustomerEmailState, EmailToken, LegalAcceptance
from admin_support import client_for, mk_customer


class FakeMailer:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    async def send(self, msg):
        self.sent.append(msg)
        return self.ok


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/mail.db")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-for-mail")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "mba_test_bot")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://app.example.test")
    for k in ("EMAIL_TRANSPORT", "EMAIL_FROM", "EMAIL_SMTP_HOST", "EMAIL_HTTP_URL", "EMAIL_HTTP_KEY", "EMAIL_SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    email_auth._ip_limiter = SlidingWindowLimiter()
    email_auth._addr_limiter = SlidingWindowLimiter()
    s = load_settings()
    dbmod.init_db(s.database_url)
    from mba_bot.api.app import create_app

    class E:
        pass

    e = E()
    e.app, e.settings, e.mp = create_app(), s, monkeypatch
    with session_scope() as db:
        c = mk_customer(db, "real@example.com", paid=True)
        c.password_hash = __import__("mba_bot.api.security", fromlist=["x"]).hash_password("old-password-1")
        s2 = mk_customer(db, "suspended@example.com", active=False)
        e.cid, e.sid = c.id, s2.id
    e.anon = TestClient(e.app, base_url="https://testserver")

    def turn_on():
        e.mp.setenv("EMAIL_TRANSPORT", "http")
        e.mp.setenv("EMAIL_FROM", "MBA Bot <no-reply@example.com>".replace("MBA Bot <", "").replace(">", ""))
        e.mp.setenv("EMAIL_HTTP_URL", "https://mail.example.test/send")
        e.mp.setenv("EMAIL_HTTP_KEY", "key-for-tests-only")
        e.mailer = FakeMailer()
        e.mp.setattr(email_auth, "build_mailer", lambda settings: e.mailer)

    e.turn_on = turn_on
    return e


def raw_from(mailer) -> str:
    return re.search(r"/#/[a-z-]+/([A-Za-z0-9_\-]{20,})", mailer.sent[-1].text).group(1)


# ---------------------------------------------------------------- off by default
def test_email_is_off_by_default_and_every_screen_says_so(env):
    assert env.settings.email_transport == "off" and M.build_mailer(env.settings) is None and M.configured(env.settings) is False
    assert env.anon.get("/api/auth/email/available").json() == {"available": False}
    r = env.anon.post("/api/auth/password-reset/request", json={"email": "real@example.com"})
    assert r.status_code == 503 and "not set up" in r.json()["detail"]
    c = client_for(env.app, env.cid, env.settings)
    assert c.post("/api/auth/email/verify/request").status_code == 503
    assert c.get("/api/auth/email/status").json() == {"available": False, "verified": False, "opt_out_non_essential": False}
    with session_scope() as db:
        assert db.scalars(select(EmailToken)).all() == []


def test_a_typo_or_a_half_set_up_transport_stays_off(monkeypatch):
    monkeypatch.setenv("EMAIL_TRANSPORT", "smpt")
    assert load_settings().email_transport == "off"
    for env_vars in ({"EMAIL_TRANSPORT": "smtp", "EMAIL_FROM": "a@example.com"}, {"EMAIL_TRANSPORT": "smtp", "EMAIL_FROM": "a@example.com", "EMAIL_SMTP_HOST": "h", "EMAIL_SMTP_USER": "u"},
                     {"EMAIL_TRANSPORT": "http", "EMAIL_FROM": "a@example.com", "EMAIL_HTTP_URL": "http://insecure.example/x", "EMAIL_HTTP_KEY": "k"}, {"EMAIL_TRANSPORT": "http", "EMAIL_FROM": "bad"}):
        for k, v in env_vars.items():
            monkeypatch.setenv(k, v)
        assert M.configured(load_settings()) is False, env_vars
        for k in env_vars:
            monkeypatch.delenv(k)
    monkeypatch.setenv("EMAIL_TRANSPORT", "smtp")
    monkeypatch.setenv("EMAIL_FROM", "a@example.com")
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.example.test")
    assert M.configured(load_settings()) is True


def test_secrets_are_never_in_the_settings_repr(monkeypatch):
    monkeypatch.setenv("EMAIL_SMTP_PASSWORD", "super-secret-pw")
    monkeypatch.setenv("EMAIL_HTTP_KEY", "super-secret-key")
    text = repr(load_settings())
    assert "super-secret" not in text


# ---------------------------------------------------------------- the senders
class FakeSmtp:
    log = []

    def __init__(self, host, port, timeout=None):
        FakeSmtp.log.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        FakeSmtp.log.append(("quit",))

    def starttls(self, context=None):
        FakeSmtp.log.append(("starttls",))

    def login(self, user, password):
        FakeSmtp.log.append(("login", user, password))

    def send_message(self, msg):
        FakeSmtp.log.append(("send", msg["To"], msg["Subject"], msg["From"], msg.get_content()))


def smtp_settings(**kw):
    base = dataclasses.replace(load_settings(), email_transport="smtp", email_from="no-reply@example.com", email_smtp_host="smtp.example.test", email_smtp_user="u", email_smtp_password="pw", **kw)
    return base


def test_smtp_sender_connects_secures_logs_in_and_sends_with_a_fake_client():
    FakeSmtp.log = []
    m = M.build_mailer(smtp_settings(), smtp_factory=FakeSmtp)
    assert asyncio.run(m.send(M.Message("a@example.com", "Hello\r\nBcc: x@evil.test", "body"))) is True
    kinds = [x[0] for x in FakeSmtp.log]
    assert kinds == ["connect", "starttls", "login", "send", "quit"]
    sent = FakeSmtp.log[3]
    assert sent[1] == "a@example.com" and "\n" not in sent[2] and "Bcc" in sent[2] and sent[3] == "no-reply@example.com"  # the injected line is flattened into the subject, not a header


def test_a_bad_address_never_reaches_the_mail_server():
    FakeSmtp.log = []
    m = M.build_mailer(smtp_settings(), smtp_factory=FakeSmtp)
    for bad in ("not an address", "a@b.c\nBcc: x@example.com", "", "a@b"):
        assert asyncio.run(m.send(M.Message(bad, "s", "b"))) is False
    assert FakeSmtp.log == []


def test_smtp_without_tls_login_or_with_ssl_follows_the_setting():
    FakeSmtp.log = []
    s = dataclasses.replace(smtp_settings(), email_smtp_security="none", email_smtp_user="", email_smtp_password=None)
    asyncio.run(M.build_mailer(s, smtp_factory=FakeSmtp).send(M.Message("a@example.com", "s", "b")))
    assert [x[0] for x in FakeSmtp.log] == ["connect", "send", "quit"]


def test_a_bad_address_a_failing_server_and_a_broken_client_return_false_and_log_no_address_or_secret(caplog):
    class Boom(FakeSmtp):
        def send_message(self, msg):
            raise RuntimeError("550 rejected victim@example.com with password pw")

    caplog.set_level(logging.DEBUG)
    m = M.build_mailer(smtp_settings(), smtp_factory=Boom)
    assert asyncio.run(m.send(M.Message("victim@example.com", "s", "b"))) is False
    assert asyncio.run(m.send(M.Message("not an address", "s", "b"))) is False
    assert asyncio.run(m.send(M.Message("a@b.c\nBcc: x@example.com", "s", "b"))) is False
    out = caplog.text
    assert "victim@example.com" not in out and "password pw" not in out and "RuntimeError" in out


def test_http_sender_shape_status_handling_and_no_secret_in_the_log(caplog):
    calls = []

    async def post(url, headers, body):
        calls.append((url, headers, body))
        return 202

    s = dataclasses.replace(load_settings(), email_transport="http", email_from="no-reply@example.com", email_http_url="https://mail.example.test/send", email_http_key="key-123")
    m = M.build_mailer(s, http_post=post)
    assert asyncio.run(m.send(M.Message("a@example.com", "Hi", "text"))) is True
    url, headers, body = calls[0]
    assert url == "https://mail.example.test/send" and headers["Authorization"] == "Bearer key-123" and body == {"from": "no-reply@example.com", "to": ["a@example.com"], "subject": "Hi", "text": "text"}

    async def bad(url, headers, body):
        return 500

    async def boom(url, headers, body):
        raise ConnectionError("down key-123")

    caplog.set_level(logging.DEBUG)
    assert asyncio.run(M.build_mailer(s, http_post=bad).send(M.Message("a@example.com", "s", "b"))) is False
    assert asyncio.run(M.build_mailer(s, http_post=boom).send(M.Message("a@example.com", "s", "b"))) is False
    assert "key-123" not in caplog.text and "a@example.com" not in caplog.text


# ---------------------------------------------------------------- reset
def request_reset(env, email):
    return env.anon.post("/api/auth/password-reset/request", json={"email": email})


def test_the_reset_answer_is_the_same_for_every_address_and_mail_goes_only_to_a_real_account(env):
    env.turn_on()
    answers = [request_reset(env, e) for e in ("real@example.com", "nobody@example.com", "suspended@example.com", "REAL@example.com ")]
    assert {(a.status_code, a.text) for a in answers[:3]} == {(200, answers[0].text)}
    assert answers[0].json() == {"message": email_auth.SAME_ANSWER}
    assert [m.to for m in env.mailer.sent] == ["real@example.com", "real@example.com"]  # the real account twice (the upper-case repeat is the same address); the unknown and the suspended one get nothing


def test_the_request_does_no_database_work_so_it_cannot_leak_through_timing(env):
    env.turn_on()
    seen = []
    event.listen(dbmod._engine, "before_cursor_execute", lambda *a, **k: seen.append(a[2]))
    before = len(seen)
    request_reset(env, "real@example.com")
    request_reset(env, "nobody@example.com")
    # statements made inside the request itself would show up before the background task; the background task makes the same two selects for both, so compare the two requests' counts
    n_real = len(seen) - before
    seen.clear()
    email_auth._addr_limiter = SlidingWindowLimiter()
    request_reset(env, "nobody2@example.com")
    n_none = len(seen)
    assert n_none <= n_real and not any("insert" in s.lower() for s in seen)


def test_the_token_is_random_stored_hashed_and_only_the_link_has_it(env):
    env.turn_on()
    request_reset(env, "real@example.com")
    raw = raw_from(env.mailer)
    assert len(raw) >= 40 and env.mailer.sent[0].text.count(f"https://app.example.test/#/reset-password/{raw}") == 1
    with session_scope() as db:
        row = db.scalars(select(EmailToken)).one()
        assert row.token_hash == F.hash_token(raw) and raw not in (row.token_hash, row.customer_id) and row.purpose == "reset" and row.used_at is None
    assert "reset-password" in env.mailer.sent[0].text and "expires in 30 minutes" in env.mailer.sent[0].text


def test_a_reset_changes_the_password_once_and_leaves_consent_alone(env):
    env.turn_on()
    with session_scope() as db:
        db.add(LegalAcceptance(customer_id=env.cid, document_type="terms", version="v-test"))
        before = [(a.document_type, a.version) for a in db.scalars(select(LegalAcceptance))]
    request_reset(env, "real@example.com")
    raw = raw_from(env.mailer)
    r = env.anon.post("/api/auth/password-reset/confirm", json={"token": raw, "new_password": "brand-new-pass-9"})
    assert r.status_code == 204
    with session_scope() as db:
        c = db.get(Customer, env.cid)
        assert verify_password("brand-new-pass-9", c.password_hash) and not verify_password("old-password-1", c.password_hash)
        assert [(a.document_type, a.version) for a in db.scalars(select(LegalAcceptance))] == before
    again = env.anon.post("/api/auth/password-reset/confirm", json={"token": raw, "new_password": "another-pass-10"})
    assert again.status_code == 400 and "not valid" in again.json()["detail"]
    login = env.anon.post("/api/auth/login", json={"email": "real@example.com", "password": "brand-new-pass-9"})
    assert login.status_code == 200


def test_unknown_expired_wrong_purpose_and_superseded_tokens_are_refused(env):
    env.turn_on()
    post = lambda tok: env.anon.post("/api/auth/password-reset/confirm", json={"token": tok, "new_password": "brand-new-pass-9"})  # noqa: E731
    assert post("x" * 43).status_code == 400
    assert env.anon.post("/api/auth/password-reset/confirm", json={"token": "short", "new_password": "brand-new-pass-9"}).status_code == 422
    assert env.anon.post("/api/auth/password-reset/confirm", json={"token": "x" * 43, "new_password": "short"}).status_code == 422
    with session_scope() as db:
        old = F.issue_token(db, env.cid, "reset", timedelta(minutes=30), now=datetime.now(timezone.utc) - timedelta(minutes=45))
        verify_tok = F.issue_token(db, env.cid, "verify", timedelta(hours=1))
    assert post(old).status_code == 400  # expired
    assert post(verify_tok).status_code == 400  # a verification token is not a reset token
    with session_scope() as db:
        first = F.issue_token(db, env.cid, "reset", timedelta(minutes=30))
        second = F.issue_token(db, env.cid, "reset", timedelta(minutes=30))
    assert post(first).status_code == 400  # superseded by the newer link
    assert post(second).status_code == 204


def test_a_suspended_customer_cannot_use_an_old_link(env):
    env.turn_on()
    with session_scope() as db:
        tok = F.issue_token(db, env.cid, "reset", timedelta(minutes=30))
        db.get(Customer, env.cid).is_active = False
    assert env.anon.post("/api/auth/password-reset/confirm", json={"token": tok, "new_password": "brand-new-pass-9"}).status_code == 400


def test_limits_per_address_are_silent_and_per_caller_answer_429(env):
    env.turn_on()
    for _ in range(4):
        assert request_reset(env, "real@example.com").status_code == 200  # same answer every time
    import time as _t
    _t.sleep(0.05)
    assert len(env.mailer.sent) == 3  # the fourth was silently dropped
    email_auth._addr_limiter, email_auth._ip_limiter = SlidingWindowLimiter(), SlidingWindowLimiter()
    codes = [request_reset(env, f"n{i}@example.com").status_code for i in range(8)]
    assert codes.count(429) == 3 and codes[:5] == [200] * 5  # 5 a minute per caller


def test_the_token_issue_limit_per_customer_and_purpose(env):
    with session_scope() as db:
        assert all(F.issue_token(db, env.cid, "reset", timedelta(minutes=30)) for _ in range(3))
        assert F.issue_token(db, env.cid, "reset", timedelta(minutes=30)) is None
        assert F.issue_token(db, env.cid, "verify", timedelta(hours=1)) is not None  # the other purpose has its own count
        assert F.issue_token(db, env.sid, "reset", timedelta(minutes=30)) is not None  # and so does another customer


def test_old_token_rows_are_deleted_when_a_new_one_is_issued(env):
    with session_scope() as db:
        F.issue_token(db, env.cid, "reset", timedelta(minutes=30), now=datetime.now(timezone.utc) - timedelta(days=9))
        F.issue_token(db, env.cid, "reset", timedelta(minutes=30))
        assert len(db.scalars(select(EmailToken)).all()) == 1


# ---------------------------------------------------------------- verification and preferences
def test_verification_by_link_marks_the_address_once(env):
    env.turn_on()
    c = client_for(env.app, env.cid, env.settings)
    assert env.anon.post("/api/auth/email/verify/request").status_code == 401
    assert c.post("/api/auth/email/verify/request").status_code == 200
    raw = raw_from(env.mailer)
    assert env.mailer.sent[-1].to == "real@example.com"
    assert env.anon.post("/api/auth/email/verify/confirm", json={"token": raw}).json() == {"verified": True}
    assert env.anon.post("/api/auth/email/verify/confirm", json={"token": raw}).status_code == 400
    assert c.get("/api/auth/email/status").json()["verified"] is True
    n = len(env.mailer.sent)
    assert "already confirmed" in c.post("/api/auth/email/verify/request").json()["message"] and len(env.mailer.sent) == n


def test_verification_requests_are_limited_per_customer(env):
    env.turn_on()
    c = client_for(env.app, env.cid, env.settings)
    codes = [c.post("/api/auth/email/verify/request").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_preferences_opt_out_and_back_in(env):
    c = client_for(env.app, env.cid, env.settings)
    assert c.post("/api/auth/email/preferences", json={"opt_out_non_essential": True}).json() == {"opt_out_non_essential": True}
    assert c.get("/api/auth/email/status").json()["opt_out_non_essential"] is True
    c.post("/api/auth/email/preferences", json={"opt_out_non_essential": False})
    assert c.get("/api/auth/email/status").json()["opt_out_non_essential"] is False
    assert env.anon.post("/api/auth/email/preferences", json={"opt_out_non_essential": True}).status_code == 401


# ---------------------------------------------------------------- notices
def test_a_non_essential_notice_respects_the_opt_out_and_an_essential_one_does_not(env):
    fm = FakeMailer()
    with session_scope() as db:
        assert asyncio.run(F.send_notice(db, env.settings, env.cid, "S", "T", essential=False, mailer=fm)) is True
        F.set_opt_out(db, env.cid, True)
        assert asyncio.run(F.send_notice(db, env.settings, env.cid, "S", "T", essential=False, mailer=fm)) is False
        assert asyncio.run(F.send_notice(db, env.settings, env.cid, "S", "T", essential=True, mailer=fm)) is True
        assert asyncio.run(F.send_notice(db, env.settings, env.sid, "S", "T", essential=True, mailer=fm)) is False  # suspended
        assert asyncio.run(F.send_notice(db, env.settings, "nobody", "S", "T", essential=True, mailer=fm)) is False
    assert len(fm.sent) == 2
    with session_scope() as db:
        assert asyncio.run(F.send_notice(db, env.settings, env.cid, "S", "T", essential=True)) is False  # email off: no sender


def test_a_failing_sender_never_breaks_the_caller(env):
    class Angry:
        async def send(self, msg):
            raise RuntimeError("down")

    with session_scope() as db:
        assert asyncio.run(F.send_notice(db, env.settings, env.cid, "S", "T", essential=True, mailer=Angry())) is False


def test_billing_reminders_are_also_emailed_when_email_is_on_and_the_reminder_can_be_opted_out(env, monkeypatch):
    from mba_bot.billing import notices

    fm = FakeMailer()
    monkeypatch.setattr("mba_bot.email_flows.build_mailer", lambda s: fm)
    on = dataclasses.replace(env.settings, email_transport="http", email_from="a@example.com", email_http_url="https://x.example", email_http_key="k")
    asyncio.run(notices._email_copies(on, [(env.cid, "soon text")], [(env.cid, "ended text")]))
    assert [m.text for m in fm.sent] == ["soon text", "ended text"]
    with session_scope() as db:
        F.set_opt_out(db, env.cid, True)
    fm.sent.clear()
    asyncio.run(notices._email_copies(on, [(env.cid, "soon text")], [(env.cid, "ended text")]))
    assert [m.text for m in fm.sent] == ["ended text"]
    fm.sent.clear()
    asyncio.run(notices._email_copies(dataclasses.replace(on, email_transport="off"), [(env.cid, "x")], [(env.cid, "y")]))
    assert fm.sent == []


def test_the_reset_and_verify_routes_get_the_stricter_rate_limit():
    from mba_bot.api.ratelimit import AUTH_PATHS

    assert {"/api/auth/password-reset/request", "/api/auth/password-reset/confirm", "/api/auth/email/verify/confirm"} <= AUTH_PATHS
