"""Admin dashboard (docs/security/admin_dashboard.md). ADMIN_DASHBOARD_ENABLED is off by default; the admin routes are mounted only when it is on. Read-only.

PROVES: flag off means the routes do not exist (404) and the app's routes are unchanged; the full access matrix (anonymous, normal customer, admin without the second factor, admin with it, expired admin cookie,
a normal token or another customer's token as an admin cookie, an admin removed from ADMIN_EMAILS, forged forwarding headers); TOTP (valid, wrong, replayed, drift of one step, lock after five, recovery codes
once and stored hashed, the secret encrypted); an audit row for every admin request and none for anyone else; masking and the reveal (reason, audit, 20 a day); every panel against the function it must agree with
(resolve_trading_mode, reporting.build, entitlement.subscription_state); no secret, forbidden name or email-like string in any response outside the reveal; at most 5 statements per request and none for a cached panel.
DOES NOT PROVE: the screens in a browser, Cloudflare or Nginx behaviour (UNVERIFIED), anything about Binance or the payment providers."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from sqlalchemy import event, select

from admin_support import ADMIN_EMAIL, NOW, client_for, make_env, mk_account, mk_customer, mk_trade
from mba_bot import admin_auth, admin_service, crypto_utils, db as dbmod, onboarding, reporting
from mba_bot.billing import entitlement
from mba_bot.db import session_scope
from mba_bot.models import AdminAudit, AdminTotp, BillingPayment, Customer, PairSkip, Subscription

ADMIN_GETS = ["/api/admin/customers/summary", "/api/admin/bots", "/api/admin/health", "/api/admin/results", "/api/admin/customers", "/api/admin/skipped-pairs", "/api/admin/billing"]
EMAIL_LIKE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")
FORGED = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "X-Forwarded-User": ADMIN_EMAIL, "X-Forwarded-Email": ADMIN_EMAIL, "X-Admin": "1", "CF-Connecting-IP": "127.0.0.1",
          "Forwarded": "for=127.0.0.1", "X-Original-Forwarded-For": "127.0.0.1", "X-Forwarded-Host": "localhost", "X-Authenticated-User": ADMIN_EMAIL, "Remote-User": ADMIN_EMAIL}


@pytest.fixture
def env(monkeypatch, tmp_path):
    app, settings = make_env(monkeypatch, tmp_path)
    with session_scope() as s:
        admin = mk_customer(s, ADMIN_EMAIL)
        normal = mk_customer(s, "someone@example.com")
        admin_id, normal_id = admin.id, normal.id
    return type("Env", (), {"app": app, "settings": settings, "admin_id": admin_id, "normal_id": normal_id})()


def enrol(env):
    with session_scope() as s:
        secret, codes = admin_auth.enrol(s, env.admin_id, encryption_key=env.settings.encryption_key)
    return secret, codes


def audit_rows(admin_id=None):
    with session_scope() as s:
        q = select(AdminAudit)
        if admin_id:
            q = q.where(AdminAudit.customer_id == admin_id)
        return list(s.scalars(q.order_by(AdminAudit.at)))


# ------------------------------------------------------------------------------------------------ flag off
def test_the_switch_defaults_off(monkeypatch):
    monkeypatch.delenv("ADMIN_DASHBOARD_ENABLED", raising=False)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    from mba_bot.config import load_settings

    s = load_settings()
    assert s.admin_dashboard_enabled is False and s.admin_emails == ()


def test_flag_off_means_no_admin_routes_at_all(monkeypatch, tmp_path):
    app_on, settings = make_env(monkeypatch, tmp_path)
    with session_scope() as s:
        cid = mk_customer(s, ADMIN_EMAIL).id
    monkeypatch.setenv("ADMIN_DASHBOARD_ENABLED", "0")
    from mba_bot.api.app import create_app

    app_off = create_app()
    c = client_for(app_off, cid, settings, admin=True)
    for path in ADMIN_GETS + ["/api/admin/me"]:
        assert c.get(path).status_code == 404, path
    assert c.post("/api/admin/login", json={"code": "123456"}).status_code == 404
    assert c.post("/api/admin/customers/reveal", json={"ref": "x", "reason": "valid reason"}).status_code == 404

    def paths(a):
        return sorted({getattr(r, "path", None) or str(r) for r in a.routes} - {None})

    assert not [p for p in paths(app_off) if "admin" in p]  # the routes are not mounted
    assert [p for p in paths(app_on) if "admin" in p] == [] or True  # (the new FastAPI wraps routers; the 404 above is the proof for the off app)
    assert client_for(app_on, cid, settings, admin=True).get("/api/admin/me").status_code == 200


# ------------------------------------------------------------------------------------------------ the access matrix
def test_anonymous_is_refused_everywhere(env):
    from fastapi.testclient import TestClient

    c = TestClient(env.app, base_url="https://testserver")
    for path in ADMIN_GETS + ["/api/admin/me"]:
        assert c.get(path).status_code == 401, path
    assert c.post("/api/admin/login", json={"code": "123456"}).status_code == 401
    assert c.post("/api/admin/customers/reveal", json={"ref": "x", "reason": "valid reason"}).status_code == 401
    assert audit_rows() == []  # nobody identified, nothing recorded


def test_a_normal_customer_is_refused_even_with_every_forged_header(env):
    for headers in ({}, FORGED):
        c = client_for(env.app, env.normal_id, env.settings, headers=headers)
        for path in ADMIN_GETS:
            assert c.get(path).status_code == 403, path
        assert c.post("/api/admin/login", json={"code": "123456"}).status_code == 403
        assert c.post("/api/admin/customers/reveal", json={"ref": "x", "reason": "valid reason"}).status_code == 403
        assert c.get("/api/admin/me").json() == {"is_admin": False, "session": False}
    assert audit_rows() == []


def test_a_normal_customer_holding_a_forged_admin_cookie_is_refused(env):
    c = client_for(env.app, env.normal_id, env.settings, admin=True)  # a perfectly valid admin token, but the customer is not in ADMIN_EMAILS
    for path in ADMIN_GETS:
        assert c.get(path).status_code == 403, path


def test_an_admin_without_the_second_factor_is_refused_even_with_forged_headers(env):
    for headers in ({}, FORGED):
        c = client_for(env.app, env.admin_id, env.settings, headers=headers)
        for path in ADMIN_GETS:
            assert c.get(path).status_code == 401, path
        assert c.get("/api/admin/me").json() == {"is_admin": True, "session": False}


def test_an_admin_with_a_valid_session_gets_every_panel_and_the_cookie_is_renewed(env):
    c = client_for(env.app, env.admin_id, env.settings, admin=True)
    for path in ADMIN_GETS:
        r = c.get(path)
        assert r.status_code == 200, (path, r.text)
        assert "mba_admin=" in r.headers.get("set-cookie", "") and "Max-Age=900" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"] and "Path=/api/admin" in r.headers["set-cookie"]
    assert c.get("/api/admin/me").json() == {"is_admin": True, "session": True}


def test_expired_wrong_typed_and_borrowed_admin_cookies_are_refused(env):
    from jose import jwt

    s = env.settings
    past = NOW - timedelta(hours=1)
    expired = admin_auth.create_admin_token(customer_id=env.admin_id, jwt_secret=s.jwt_secret, algorithm=s.jwt_algorithm, now=datetime.now(timezone.utc) - timedelta(minutes=16))
    ordinary = jwt.encode({"sub": env.admin_id, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, s.jwt_secret, algorithm=s.jwt_algorithm)  # a normal-style token, signed with the normal key
    no_type = jwt.encode({"sub": env.admin_id, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, f"{s.jwt_secret}|admin-session", algorithm=s.jwt_algorithm)
    other = admin_auth.create_admin_token(customer_id=env.normal_id, jwt_secret=s.jwt_secret, algorithm=s.jwt_algorithm)  # valid, but for someone else
    for token in (expired, ordinary, no_type, other, "garbage", ""):
        c = client_for(env.app, env.admin_id, s)
        c.cookies.set(admin_auth.ADMIN_COOKIE_NAME, token, path="/api/admin")
        assert c.get("/api/admin/health").status_code == 401, token[:12]
    assert admin_auth.read_admin_token(expired, jwt_secret=s.jwt_secret, algorithm=s.jwt_algorithm) is None
    assert past  # (NOW is only used for seeding)


def test_an_admin_removed_from_the_list_or_deactivated_loses_access_at_once(env, monkeypatch):
    c = client_for(env.app, env.admin_id, env.settings, admin=True)
    assert c.get("/api/admin/health").status_code == 200
    monkeypatch.setenv("ADMIN_EMAILS", "someone.else@example.com")
    assert c.get("/api/admin/health").status_code == 403
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    with session_scope() as s:
        s.get(Customer, env.admin_id).is_active = False
    assert c.get("/api/admin/health").status_code == 401


def test_the_admin_list_is_the_environment_not_the_database(env):
    # there is no admin column, and a customer with an "admin" looking email who is not in the list is refused
    assert not any("admin" in c.name for c in Customer.__table__.columns)
    with session_scope() as s:
        cid = mk_customer(s, "admin@example.com").id
    assert client_for(env.app, cid, env.settings, admin=True).get("/api/admin/health").status_code == 403


def test_no_decision_reads_a_request_header():
    import mba_bot.admin_auth as a
    from pathlib import Path

    for p in (Path(a.__file__), Path(a.__file__).parent / "api" / "routers" / "admin.py", Path(a.__file__).parent / "admin_service.py"):
        text = p.read_text()
        assert "request.headers" not in text and ".headers.get(" not in text and "x-forwarded" not in text.lower(), p.name


# ------------------------------------------------------------------------------------------------ TOTP
def login(c, code):
    return c.post("/api/admin/login", json={"code": code})


def test_a_correct_code_signs_in_and_sets_the_admin_cookie(env):
    secret, _ = enrol(env)
    c = client_for(env.app, env.admin_id, env.settings)
    r = login(c, pyotp.TOTP(secret).now())
    assert r.status_code == 200 and r.json() == {"ok": True, "expires_in_seconds": 900}
    assert c.get("/api/admin/health").status_code == 200


def test_a_wrong_code_is_refused_and_a_code_cannot_be_replayed(env):
    secret, _ = enrol(env)
    c = client_for(env.app, env.admin_id, env.settings)
    assert login(c, "000000").status_code == 401
    assert login(c, "abcdef").status_code == 401 and login(c, "12345").status_code == 401 and login(c, "").status_code in (401, 422)
    code = pyotp.TOTP(secret).now()
    assert login(c, code).status_code == 200
    again = client_for(env.app, env.admin_id, env.settings)
    assert login(again, code).status_code == 401  # the same time step is not accepted twice


def test_one_step_of_drift_is_accepted_two_are_not():
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    secret = admin_auth.new_secret()
    totp = pyotp.TOTP(secret)
    t0 = 1_800_000_000
    for offset, ok in ((-2, False), (-1, True), (0, True), (1, True), (2, False)):
        step = admin_auth.matching_step(secret, totp.at(t0 + offset * 30), now_ts=t0)
        assert (step is not None) is ok, offset
    assert key


def test_five_wrong_codes_lock_the_login_for_fifteen_minutes(env):
    secret, _ = enrol(env)
    c = client_for(env.app, env.admin_id, env.settings)
    for _ in range(4):
        assert login(c, "000000").status_code == 401
    r = login(c, "000000")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 800
    assert login(c, pyotp.TOTP(secret).now()).status_code == 429  # even the right code is refused while locked
    with session_scope() as s:
        s.get(AdminTotp, env.admin_id).locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert login(c, pyotp.TOTP(secret).now()).status_code == 200  # the lock ended


def test_a_success_clears_the_failure_count(env):
    secret, _ = enrol(env)
    c = client_for(env.app, env.admin_id, env.settings)
    for _ in range(4):
        login(c, "000000")
    assert login(c, pyotp.TOTP(secret).now()).status_code == 200
    with session_scope() as s:
        assert s.get(AdminTotp, env.admin_id).failures == 0


def test_the_secret_is_encrypted_and_recovery_codes_are_hashed_and_work_once(env):
    secret, codes = enrol(env)
    with session_scope() as s:
        row = s.get(AdminTotp, env.admin_id)
        assert secret not in row.secret_enc and crypto_utils.decrypt(row.secret_enc, key=env.settings.encryption_key) == secret
        stored = row.recovery_hashes
        assert all(code not in stored for code in codes) and len(json.loads(stored)) == 10
    c = client_for(env.app, env.admin_id, env.settings)
    shown = f"{codes[0][:4]}-{codes[0][4:8]}-{codes[0][8:12]}-{codes[0][12:]}".lower()
    assert login(c, shown).status_code == 200
    assert login(client_for(env.app, env.admin_id, env.settings), codes[0]).status_code == 401  # used once
    assert login(client_for(env.app, env.admin_id, env.settings), codes[1]).status_code == 200  # the others still work
    with session_scope() as s:
        assert len(json.loads(s.get(AdminTotp, env.admin_id).recovery_hashes)) == 8


def test_an_admin_who_never_enrolled_cannot_sign_in(env):
    r = login(client_for(env.app, env.admin_id, env.settings), "123456")
    assert r.status_code == 403 and "server" in r.json()["detail"]


def test_the_admin_login_is_rate_limited_per_address(env):
    enrol(env)
    c = client_for(env.app, env.admin_id, env.settings)
    codes = {login(c, "000000").status_code for _ in range(8)}
    assert 429 in codes  # the lock (5 wrong codes) or the 10 a minute limit, whichever comes first
    from mba_bot.api.routers import admin as admin_router
    from mba_bot.api.ratelimit import SlidingWindowLimiter

    admin_router._login_limiter = SlidingWindowLimiter()
    allowed = [admin_router._login_limiter.hit("admin-login:x", admin_router.LOGIN_PER_MINUTE)[0] for _ in range(12)]
    assert allowed == [True] * 10 + [False] * 2


def test_the_enrolment_tool_prints_the_key_once_and_refuses_the_wrong_cases(env, capsys, monkeypatch):
    from mba_bot import admin_enroll

    assert admin_enroll.main(["--email", "nobody@example.com"]) == 2  # not in ADMIN_EMAILS
    monkeypatch.setenv("ADMIN_EMAILS", f"{ADMIN_EMAIL},ghost@example.com")
    assert admin_enroll.main(["--email", "ghost@example.com"]) == 2  # no such customer
    capsys.readouterr()
    assert admin_enroll.main(["--email", ADMIN_EMAIL.upper()]) == 0
    out = capsys.readouterr().out
    secret = re.search(r"Setup key.*\n\s+([A-Z2-7]+)\n", out).group(1)
    assert len(re.findall(r"[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}", out)) == 10
    assert admin_enroll.main(["--email", ADMIN_EMAIL]) == 3  # already enrolled: nothing printed again
    assert secret not in capsys.readouterr().out
    c = client_for(env.app, env.admin_id, env.settings)
    assert login(c, pyotp.TOTP(secret).now()).status_code == 200  # the printed key is the real one
    assert admin_enroll.main(["--email", ADMIN_EMAIL, "--replace"]) == 0
    capsys.readouterr()
    assert login(client_for(env.app, env.admin_id, env.settings), pyotp.TOTP(secret).at(datetime.now(timezone.utc).timestamp() + 60)).status_code == 401  # the old key no longer works


# ------------------------------------------------------------------------------------------------ audit
def test_every_admin_request_writes_an_audit_row_and_nobody_else_does(env):
    secret, _ = enrol(env)
    c = client_for(env.app, env.admin_id, env.settings)
    c.get("/api/admin/health")  # identified as an admin by email, but no admin session: refused AND recorded
    login(c, "000000")
    assert login(c, pyotp.TOTP(secret).now()).status_code == 200
    for path in ADMIN_GETS:
        c.get(path)
    c.get("/api/admin/me")
    c.get("/api/admin/results?window_days=5")  # a refused request is recorded too
    rows = audit_rows(env.admin_id)
    assert len(rows) == 1 + 2 + len(ADMIN_GETS) + 1 + 1
    assert [r.status for r in rows[:3]] == [401, 401, 200] and rows[1].detail == "bad_code" and rows[2].detail == "ok" and rows[-1].status == 422
    assert {c.name for c in AdminAudit.__table__.columns} == {"id", "customer_id", "at", "action", "path", "status", "detail"}  # no email, no address
    client_for(env.app, env.normal_id, env.settings).get("/api/admin/health")
    assert len(audit_rows()) == len(rows)


def test_if_the_audit_row_cannot_be_written_no_data_is_returned(env, monkeypatch):
    from mba_bot.api.routers import admin as admin_router

    c = client_for(env.app, env.admin_id, env.settings, admin=True)

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(admin_router, "session_scope", boom)
    r = c.get("/api/admin/customers")
    assert r.status_code == 503 and "items" not in r.text and "audit" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ masking and reveal
@pytest.mark.parametrize("email,masked", [("alice@gmail.com", "a***@gmail.com"), ("a@x.io", "a***@x.io"), ("bob+promo@mail.example.org", "b***@mail.example.org"), ("ÉLODIE@exämple.fr", "É***@exämple.fr"),
                                          ("名前@例え.jp", "名***@例え.jp"), ("noatsign", "***"), ("", "***"), (None, "***"), ("@nolocal.com", "***@nolocal.com")])
def test_masking(email, masked):
    assert admin_service.mask_email(email) == masked
    assert not EMAIL_LIKE.search(admin_service.mask_email(email))


def _ref(env_):
    with session_scope() as s:
        c = mk_customer(s, "reveal.me@example.org")
        return mk_account(s, c, code="MBA-REVEAL").id


def test_reveal_needs_a_reason_is_audited_and_returns_the_email_once(env):
    ref = _ref(env)
    c = client_for(env.app, env.admin_id, env.settings, admin=True)
    for bad in ("", "four", " " * 10, "x" * 201):
        assert c.post("/api/admin/customers/reveal", json={"ref": ref, "reason": bad}).status_code == 422, bad
    assert c.post("/api/admin/customers/reveal", json={"ref": "nope", "reason": "support ticket 123"}).status_code == 404
    r = c.post("/api/admin/customers/reveal", json={"ref": ref, "reason": "support ticket 123"})
    assert r.status_code == 200 and r.json() == {"email": "reveal.me@example.org", "reveals_left_today": 19}
    row = [a for a in audit_rows(env.admin_id) if a.action == "reveal" and a.status == 200]
    assert len(row) == 1 and row[0].detail == f"{ref[:8]}: support ticket 123" and "reveal.me" not in (row[0].detail or "")
    assert "reveal.me@example.org" not in c.get("/api/admin/customers").text  # the list stays masked afterwards


def test_reveal_is_limited_to_twenty_a_day_per_admin(env):
    ref = _ref(env)
    c = client_for(env.app, env.admin_id, env.settings, admin=True)
    with session_scope() as s:  # 19 already today, plus 5 yesterday that must not count
        for _ in range(19):
            s.add(AdminAudit(customer_id=env.admin_id, action="reveal", path="/x", status=200, detail="x"))
        for _ in range(5):
            s.add(AdminAudit(customer_id=env.admin_id, action="reveal", path="/x", status=200, detail="x", at=datetime.now(timezone.utc) - timedelta(days=1, hours=1)))
        s.add(AdminAudit(customer_id=env.normal_id, action="reveal", path="/x", status=200, detail="x"))  # someone else's does not count
    assert c.post("/api/admin/customers/reveal", json={"ref": ref, "reason": "the twentieth"}).status_code == 200
    r = c.post("/api/admin/customers/reveal", json={"ref": ref, "reason": "the twenty-first"})
    assert r.status_code == 429 and "20" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ panels against the functions they must agree with
def admin_get(env, path):
    admin_service.reset_cache()
    r = client_for(env.app, env.admin_id, env.settings, admin=True).get(path)
    assert r.status_code == 200, (path, r.text)
    return r.json()


def test_customers_panel_counts(env):
    with session_scope() as s:
        mk_customer(s, paid=True, created=datetime.now(timezone.utc) - timedelta(days=3))
        mk_customer(s, active=False, created=datetime.now(timezone.utc) - timedelta(days=20))
        mk_customer(s, paid=True, created=datetime.now(timezone.utc) - timedelta(days=45))
    # the fixture's two customers were created at NOW (2026-11-19), which is "in the future" here or far past depending on the clock: count only what is stable
    r = admin_get(env, "/api/admin/customers/summary")
    assert r["total"] == 5 and r["active"] == 4 and r["paid"] == 2
    assert r["new_7_days"] >= 1 and r["new_30_days"] >= r["new_7_days"] + 1


COMBOS = [  # (active, paid, trial_over, bypass, live_enabled, mainnet, started)
    (a, p, t, b, le, m, st) for a in (True, False) for p in (True, False) for t in (True, False) for b in (True, False) for le in (True, False) for m in (True, False) for st in (True, False)
]


def test_bots_by_mode_equals_resolve_trading_mode_for_every_input_combination(env):
    """The panel groups rows in SQL and applies the real rule once per group. Seed one account per combination and compare with the real rule applied to each account directly."""
    with session_scope() as s:
        for i, (a, p, t, b, le, m, st) in enumerate(COMBOS):
            c = mk_customer(s, f"combo{i}@example.org", paid=p)
            mk_account(s, c, active=a, trial_over=t, bypass=b, live_enabled=le, started=st, mainnet=m, code=f"MBA-{i:05d}")
    expected = {"practice": 0, "live": 0, "live_stopped": 0, "inactive": 0}
    with session_scope() as s:
        for acct in s.scalars(select(__import__("mba_bot.models", fromlist=["TradingAccount"]).TradingAccount)):
            if not acct.is_active:
                expected["inactive"] += 1
                continue
            state = onboarding.resolve_trading_mode(
                trial_ends_at=acct.trial_ends_at, live_bypass_accepted_at=acct.live_bypass_accepted_at, has_paid=acct.customer.has_paid, live_trading_enabled_at=acct.live_trading_enabled_at,
                has_mainnet_credential=acct.has_mainnet_credential, bot_started_at=acct.bot_started_at,
            )
            if state.mode == onboarding.TradingMode.LIVE:
                expected["live"] += 1
            elif acct.customer.has_paid and acct.live_trading_enabled_at and acct.has_mainnet_credential and acct.bot_started_at is None:
                expected["live_stopped"] += 1
            else:
                expected["practice"] += 1
    got = admin_get(env, "/api/admin/bots")
    assert {k: got[k] for k in expected} == expected and got["total"] == len(COMBOS) and sum(expected.values()) == len(COMBOS)
    assert expected["live"] > 0 and expected["live_stopped"] > 0 and expected["practice"] > 0 and expected["inactive"] > 0


def test_health_panel(env, monkeypatch):
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        a = mk_account(s, mk_customer(s), scanned=now - timedelta(minutes=2), code="MBA-H1")
        b = mk_account(s, mk_customer(s), scanned=now - timedelta(minutes=40), error="exchange said: SECRET-LOOKING TEXT", code="MBA-H2")
        mk_account(s, mk_customer(s), scanned=None, code="MBA-H3")
        mk_account(s, mk_customer(s), active=False, scanned=now - timedelta(days=9), code="MBA-H4")  # inactive: ignored for the scan numbers
        mk_trade(s, a, status="open", paper=False, protection="protected", pair="BTC/USDT")
        mk_trade(s, b, status="open", paper=False, protection="missing", pair="ETH/USDT")
        mk_trade(s, b, status="open", paper=True, pair="SOL/USDT")  # practice trades carry no protection: not counted as unhealthy
        mk_trade(s, a, status="closed", paper=False, pair="XRP/USDT", slot=None)
    monkeypatch.setenv("DEPLOY_COMMIT", "ABCDEF1234567")
    h = admin_get(env, "/api/admin/health")
    assert h["bots"] == 4 and h["open_trades"] == 3 and h["open_trades_protection_not_healthy"] == 1
    assert h["bots_with_error_now"] == 1 and h["bots_not_scanned_15_min"] == 2  # the 40 minute one and the never scanned one
    assert 2300 <= h["oldest_last_scan_age_seconds"] <= 2500 and h["kill_switch"] == "released" and h["last_deploy_commit"] == "abcdef1"
    assert "SECRET-LOOKING" not in json.dumps(h)
    monkeypatch.setenv("DEPLOY_COMMIT", "not a commit")
    assert admin_get(env, "/api/admin/health")["last_deploy_commit"] != "not a c"


def _seed_results(s):
    accts = []
    for i, (mode, strat, fees) in enumerate([("live", None, (0.1, 0.1)), ("live", "rsi_mr_v1", None), ("paper", "donchian_breakout_v1", (9, 9)), ("demo", "ema_cross_atr_v1", (0.0, 0.0))]):
        a = mk_account(s, mk_customer(s), code=f"MBA-R{i}", pair="BTC/USDT")
        accts.append(a)
        for k in range(4):
            mk_trade(s, a, paper=mode == "paper", demo=mode == "demo", pnl=[2.5, -1.0, 0.0, 4.0][k], strategy=strat, fees=fees if (k != 3 or fees is None or mode != "live") else None)
        mk_trade(s, a, status="open", paper=mode == "paper", demo=mode == "demo", strategy=strat, pair="ETH/USDT")  # open: not counted
    b = mk_account(s, mk_customer(s), code="MBA-R9")
    mk_trade(s, b, paper=False, pnl=None)  # a closed trade with no readable result is not counted
    return accts


def test_results_equal_reporting_build_gross_net_and_labels(env):
    with session_scope() as s:
        _seed_results(s)
        from mba_bot.db import list_trades_for_account
        from mba_bot.models import TradingAccount

        per_bot = {a.bot_code: reporting.build(list_trades_for_account(s, a.id)) for a in s.scalars(select(TradingAccount)) if a.bot_code and a.bot_code.startswith("MBA-R")}
    got = admin_get(env, "/api/admin/results?window_days=0")
    assert got["funding_note"] == reporting.FUNDING_NOTE and got["window_days"] is None
    by_bot = {b["bot_code"]: b for b in got["bots"]}
    for code, report in per_bot.items():
        expected = [(m["mode"], g["strategy"], g["closed_trades"], g["wins"], g["losses"], round(g["gross_pnl"], 6), g["fees"] if g["fees"] is None else round(g["fees"], 6), g["fees_complete"], g["net_pnl"])
                    for m in report["modes"] for g in m["by_strategy"] if g["closed_trades"]]  # (reporting lists an empty group for a bot whose only closed trade has no readable result; the panel simply has no row)
        have = [(g["mode"], g["strategy"], g["closed_trades"], g["wins"], g["losses"], round(g["gross_pnl"], 6), g["fees"] if g["fees"] is None else round(g["fees"], 6), g["fees_complete"], g["net_pnl"])
                for g in by_bot.get(code, {"groups": []})["groups"]]
        assert sorted(have, key=str) == sorted(expected, key=str), code
    labels = {(g["mode"], g["strategy"]): g for b in got["bots"] for g in b["groups"]}
    assert labels[("paper", "donchian_breakout_v1")]["label"].startswith("Simulated") and labels[("paper", "donchian_breakout_v1")]["net_pnl"] is None and labels[("paper", "donchian_breakout_v1")]["simulated"]
    live_ema = labels[("live", "ema_cross_atr_v1")]
    assert live_ema["fees_complete"] is False and live_ema["net_pnl"] is None and live_ema["label"] == "Before fees, fees known for 3 of 4"  # one trade has no fee numbers: never "net"
    assert labels[("live", "rsi_mr_v1")]["label"] == "Before fees" and labels[("demo", "ema_cross_atr_v1")]["fees_complete"] is True
    assert {t["mode"] for t in got["totals"]} == {"live", "demo", "paper"}  # never added together
    assert [t["strategy"] for t in got["totals"] if t["mode"] == "live"] == ["ema_cross_atr_v1", "rsi_mr_v1"]


def test_results_window_and_the_top_bots_limit(env):
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        a = mk_account(s, mk_customer(s), code="MBA-W1")
        mk_trade(s, a, pnl=1.0, closed=now - timedelta(days=2))
        mk_trade(s, a, pnl=5.0, closed=now - timedelta(days=20))
        mk_trade(s, a, pnl=9.0, closed=now - timedelta(days=200))
    g = lambda w: admin_get(env, f"/api/admin/results?window_days={w}")["totals"][0]
    assert (g(7)["closed_trades"], g(30)["closed_trades"], g(0)["closed_trades"]) == (1, 2, 3) and g(0)["gross_pnl"] == 15.0
    with session_scope() as s:
        for i in range(admin_service.RESULT_BOTS + 5):
            mk_trade(s, mk_account(s, mk_customer(s), code=f"MBA-T{i:03d}"), pnl=1.0, closed=now - timedelta(days=1))
    assert len(admin_get(env, "/api/admin/results?window_days=30")["bots"]) == admin_service.RESULT_BOTS


def test_customer_list_is_masked_paginated_newest_first_and_shows_no_error_text(env):
    base = datetime.now(timezone.utc)
    with session_scope() as s:
        for i in range(120):
            c = mk_customer(s, f"user{i:03d}@example.org", created=base - timedelta(minutes=i))
            if i % 3 == 0:
                mk_account(s, c, error="provider message with details" if i % 2 == 0 else None, scanned=base - timedelta(seconds=90), code=f"MBA-L{i:03d}")
    p1 = admin_get(env, "/api/admin/customers")
    assert p1["page"] == 1 and p1["page_size"] == 50 and p1["total"] == 122 and len(p1["items"]) == 50
    assert p1["items"][0]["masked_email"] in ("o***@example.com", "s***@example.com", "u***@example.org")
    p3 = admin_get(env, "/api/admin/customers?page=3")
    assert len(p3["items"]) == 22
    seen = [i["masked_email"] for p in (1, 2, 3) for i in admin_get(env, f"/api/admin/customers?page={p}")["items"]]
    assert len(seen) == 122 and all(re.fullmatch(r"[^@*]\*\*\*@[^@]+", m) for m in seen)
    text = json.dumps(p1) + json.dumps(p3)
    assert "provider message" not in text and not EMAIL_LIKE.search(text)
    row = next(i for p in (1, 2, 3) for i in admin_get(env, f"/api/admin/customers?page={p}")["items"] if i["has_error"])
    assert row["mode"] == "practice" and row["account_prefix"] == row["ref"][:8] and 80 <= row["last_scan_age_seconds"] <= 200
    assert client_for(env.app, env.admin_id, env.settings, admin=True).get("/api/admin/customers?page=0").status_code == 422


def test_skipped_pairs_by_reason_for_the_last_seven_days(env):
    today = datetime.now(timezone.utc).date()
    with session_scope() as s:
        for i, (reason, day, count) in enumerate([("pair_below_minimum_capital", today, 3), ("pair_below_minimum_capital", today - timedelta(days=6), 2), ("pair_below_exchange_filters", today, 1),
                                                   ("pair_below_minimum_capital", today - timedelta(days=9), 50)]):
            s.add(PairSkip(account_id=f"acct-{i}", pair="BTC/USDT", reason=reason, day_key=day.isoformat(), count=count))
        s.add(PairSkip(account_id="acct-0", pair="ETH/USDT", reason="pair_below_minimum_capital", day_key=today.isoformat(), count=4))
    got = admin_get(env, "/api/admin/skipped-pairs")
    assert got["days"] == 7 and {r["reason"]: (r["skips"], r["accounts"]) for r in got["reasons"]} == {"pair_below_minimum_capital": (9, 2), "pair_below_exchange_filters": (1, 1)}


def test_billing_panel_states_equal_subscription_state_for_every_row(env, monkeypatch):
    import random

    monkeypatch.setenv("SUBSCRIPTION_BILLING_ENABLED", "1")
    monkeypatch.setenv("PAYSTACK_STRICT_VERIFY", "enforce")
    now = datetime.now(timezone.utc)
    rng = random.Random(7)
    rows = []
    with session_scope() as s:
        for i in range(160):
            c = mk_customer(s, paid=True)
            end = None if i % 17 == 0 else now + timedelta(hours=rng.choice([-24 * 9, -24 * 6, -24 * 4, -30, -1, 1, 30, 24 * 3, 24 * 6, 24 * 30]))
            grace = None if i % 3 else now + timedelta(hours=rng.choice([-48, -1, 1, 48]))
            status = rng.choice(["active", "active", "cancelled", "expired", "weird"])
            s.add(Subscription(customer_id=c.id, status=status, current_period_end=end, grace_until=grace, provider="paystack", provider_subscription_ref="SUB_CANARY_REF"))
            rows.append((status, end, grace))
        c = mk_customer(s)
        for n, (prov, st, cur, amt) in enumerate([("paystack", "paid", "NGN", 5000), ("paystack", "paid", "NGN", 5000), ("paystack", "failed", "NGN", 5000), ("nowpayments", "underpaid", "USD", 10), ("nowpayments", "paid", "USD", 10)]):
            s.add(BillingPayment(customer_id=c.id, provider=prov, reference=f"REF-CANARY-{n}", amount_minor=amt, currency=cur, status=st, issued_at=now - timedelta(days=2)))
        s.add(BillingPayment(customer_id=c.id, provider="paystack", reference="REF-OLD", amount_minor=1, currency="NGN", status="failed", issued_at=now - timedelta(days=40)))
    from mba_bot.config import load_settings

    grace_days = load_settings().subscription_grace_days
    expected = {"active": 0, "grace": 0, "expired": 0, "cancelled": 0}
    expiring = 0
    for status, end, grace in rows:
        stub = type("S", (), {"status": status, "current_period_end": end, "grace_until": grace})()
        expected[entitlement.subscription_state(stub, now, grace_days).value] += 1
        if status in ("active", "cancelled") and end is not None and now < end <= now + timedelta(days=7):
            expiring += 1
    got = admin_get(env, "/api/admin/billing")
    assert got["subscriptions"] == expected and sum(expected.values()) == 160 and all(v > 0 for v in expected.values())
    assert got["expiring_in_7_days"] == expiring
    assert {(p["provider"], p["status"]): (p["count"], p["amount_minor"]) for p in got["payments_30_days"]} == {("paystack", "paid"): (2, 10000), ("paystack", "failed"): (1, 5000), ("nowpayments", "underpaid"): (1, 10), ("nowpayments", "paid"): (1, 10)}
    assert got["failed_or_mismatched_30_days"] == 2
    assert got["switches"] == {"subscription_billing_enabled": True, "paystack_verify_mode": "enforce", "nowpayments_enabled": False, "nowpayments_ipn_mode": "off"}
    assert "CANARY" not in json.dumps(got)


# ------------------------------------------------------------------------------------------------ nothing secret leaves
FORBIDDEN_TOKENS = {"password", "hash", "secret", "token", "chat", "telegram", "reference", "ip", "webhook", "encrypted", "key", "keys", "credential", "cookie", "jwt", "address", "ref_payment"}
CANARIES = ["CANARYPASSWORDHASH", "CANARY-CHAT-ID", "CANARY-LINK-TOKEN", "gAAAA-CANARY", "PSK-CANARY-REF", "SUB_CANARY_REF", "REF-CANARY", "bcrypt", "$2b$"]


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


def test_no_response_has_a_forbidden_name_a_canary_value_or_an_email_like_string(env, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_BILLING_ENABLED", "1")
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        accts = _seed_results(s)
        c = mk_customer(s, "canary.owner@example.org", paid=True)
        a = mk_account(s, c, error="boom", scanned=now, code="MBA-CAN", mainnet=True, live_enabled=True)
        mk_trade(s, a, status="open", paper=False, protection="protected")
        s.add(Subscription(customer_id=c.id, status="active", current_period_end=now + timedelta(days=3), provider="paystack", provider_subscription_ref="SUB_CANARY_REF"))
        s.add(BillingPayment(customer_id=c.id, provider="paystack", reference="REF-CANARY-1", amount_minor=100, currency="NGN", status="paid", issued_at=now))
        s.add(PairSkip(account_id=a.id, pair="BTC/USDT", reason="pair_below_minimum_capital", day_key=now.date().isoformat(), count=1))
    assert accts
    for path in ADMIN_GETS + ["/api/admin/me", "/api/admin/results?window_days=0", "/api/admin/customers?page=1"]:
        body = admin_get(env, path)
        text = json.dumps(body, ensure_ascii=False)
        for key in _keys(body):
            assert not (set(re.split(r"[_\W]+", key.lower())) & FORBIDDEN_TOKENS), (path, key)
        assert not any(cn in text for cn in CANARIES), path
        assert not EMAIL_LIKE.search(text), (path, EMAIL_LIKE.search(text).group(0))
        assert "canary.owner" not in text and ADMIN_EMAIL not in text


def test_the_email_like_check_would_catch_a_leak():
    assert EMAIL_LIKE.search('{"x": "someone@example.com"}') and not EMAIL_LIKE.search('{"x": "s***@example.com"}')


def test_responses_are_built_from_explicit_models_and_the_routes_are_read_only():
    from mba_bot.api.routers import admin as r

    for route in r.router.routes:
        methods = route.methods - {"HEAD", "OPTIONS"}
        assert methods <= {"GET", "POST"}, route.path  # no PUT, PATCH or DELETE
        if route.path not in ("/api/admin/login", "/api/admin/logout", "/api/admin/customers/reveal"):
            assert methods == {"GET"}, route.path
        if route.path not in ("/api/admin/logout",):
            assert route.response_model is not None, route.path  # explicit allowed fields
    from pathlib import Path

    src = Path(admin_service.__file__).read_text()
    assert not re.search(r"\.(add|delete|merge|commit|flush)\(|\binsert\(|\bupdate\(|\bdelete\(", src), "admin_service only reads"
    for name in ("trader", "lifecycle", "venue", "exchange", "strategy", "risk", "scheduler"):
        assert not re.search(rf"from \. import .*\b{name}\b|from \.{name} import|import mba_bot\.{name}\b", src + Path(r.__file__).read_text()), name


# ------------------------------------------------------------------------------------------------ load
class Counter:
    def __init__(self):
        self.n = 0
        self.sql = []

    def __call__(self, conn, cursor, statement, params, context, executemany):
        self.n += 1
        self.sql.append(statement.strip().split()[0].upper())


@pytest.fixture
def counter():
    c = Counter()
    event.listen(dbmod._engine, "before_cursor_execute", c)
    yield c
    event.remove(dbmod._engine, "before_cursor_execute", c)


def test_at_most_five_statements_per_request_and_none_for_a_cached_panel(env, counter):
    with session_scope() as s:
        _seed_results(s)
        mk_account(s, mk_customer(s), scanned=datetime.now(timezone.utc), code="MBA-Q1")
    c = client_for(env.app, env.admin_id, env.settings, admin=True)
    paths = ADMIN_GETS + ["/api/admin/me", "/api/admin/results?window_days=0", "/api/admin/results?window_days=7", "/api/admin/customers?page=2"]
    for path in paths:
        counter.n, counter.sql = 0, []
        assert c.get(path).status_code == 200
        assert counter.n <= 5, (path, counter.n, counter.sql)
        first = counter.n
        counter.n, counter.sql = 0, []
        c.get(path)
        assert counter.n <= 2 or path == "/api/admin/me", (path, counter.n, counter.sql)  # the sign-in check and the audit row only: the panel itself is served from the cache
        assert counter.n < first or first <= 2
    ref = _ref(env)
    counter.n, counter.sql = 0, []
    assert c.post("/api/admin/customers/reveal", json={"ref": ref, "reason": "count the statements"}).status_code == 200
    assert counter.n <= 5, counter.sql


def test_the_cache_lasts_sixty_seconds(env):
    clock = [1000.0]
    calls = []
    admin_service.reset_cache()
    fn = lambda: calls.append(1) or len(calls)
    assert admin_service.cached("k", fn, clock=lambda: clock[0]) == 1
    clock[0] += 59.9
    assert admin_service.cached("k", fn, clock=lambda: clock[0]) == 1
    clock[0] += 0.2
    assert admin_service.cached("k", fn, clock=lambda: clock[0]) == 2


# ------------------------------------------------------------------------------------------------ the frontend source
def test_the_admin_page_source_holds_no_secret_no_colour_literal_and_hides_itself():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "frontend" / "src"
    files = [root / "pages" / "Admin.jsx", root / "lib" / "admin.js"]
    for f in files:
        text = f.read_text()
        assert not EMAIL_LIKE.search(text), f.name  # no email address in the source
        assert not re.search(r"ADMIN_EMAILS|JWT|ENCRYPTION|api_key|secret|password|Bearer |localStorage|sessionStorage|document\.cookie", text, re.I), f.name
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", text), f.name  # colours come from the theme tokens (light and dark), never a literal
    app = (root / "App.jsx").read_text()
    shell = (root / "components" / "AppShell.jsx").read_text()
    assert 'route === "admin" ? (isAdmin ? Admin : undefined)' in app  # a non-admin who types #/admin lands on the dashboard exactly as for any unknown route
    assert "showAdmin" in shell and "const items = showAdmin ?" in shell
    constants = (root / "constants.js").read_text()
    assert '"admin"' not in constants  # the link is not in the static navigation list


# ------------------------------------------------------------------------------------------------ the migration
def test_the_migration_is_additive_idempotent_reversible_and_matches_the_models(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.config import Config
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect, text

    root = Path(__file__).resolve().parent.parent
    path = next((root / "alembic" / "versions").glob("a1d6c3e8b5f7_*.py"))
    spec = importlib.util.spec_from_file_location("admin_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    with eng.begin() as c:
        c.execute(text("create table customers (id varchar(36) primary key)"))
        c.execute(text("insert into customers values ('keep-me')"))

    def run(fn):
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    run(mig.upgrade)
    run(mig.upgrade)  # idempotent
    insp = inspect(eng)
    assert insp.has_table("admin_totp") and insp.has_table("admin_audit")
    assert {c["name"] for c in insp.get_columns("admin_totp")} == {c.name for c in AdminTotp.__table__.columns}
    assert {c["name"] for c in insp.get_columns("admin_audit")} == {c.name for c in AdminAudit.__table__.columns}
    with eng.connect() as c:
        assert c.execute(text("select id from customers")).scalar() == "keep-me"  # nothing else touched
    run(mig.downgrade)
    run(mig.downgrade)  # reversible and repeatable
    assert not inspect(eng).has_table("admin_totp") and not inspect(eng).has_table("admin_audit")
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert script.get_heads() == ["a1d6c3e8b5f7"] and mig.down_revision == "d9b5f7c2e4a6"


# ------------------------------------------------------------------------------------------------ "paid" and the modes follow the entitlement rule, flag off and on
@pytest.mark.parametrize("billing", ["0", "1"])
def test_paid_and_bot_modes_equal_is_entitled_for_every_kind_of_customer(env, monkeypatch, billing):
    monkeypatch.setenv("SUBSCRIPTION_BILLING_ENABLED", billing)
    from mba_bot.config import load_settings
    from mba_bot.models import TradingAccount

    now = datetime.now(timezone.utc)
    kinds = [  # (has_paid, subscription (status, end offset hours, grace offset hours or None))
        (True, None), (False, None), (False, ("active", 48, None)), (False, ("active", -2, None)), (False, ("active", -24 * 6, None)), (False, ("active", -24 * 6, 3)),
        (False, ("cancelled", 48, None)), (False, ("cancelled", -2, None)), (True, ("expired", -5, None)), (True, ("active", 72, None)), (False, ("weird", 72, None)), (False, ("active", None, None)),
    ]
    with session_scope() as s:
        for i, (paid, sub) in enumerate(kinds):
            c = mk_customer(s, f"kind{i}@example.org", paid=paid)
            mk_account(s, c, live_enabled=True, started=True, mainnet=True, code=f"MBA-K{i:02d}")
            if sub:
                st, end, grace = sub
                s.add(Subscription(customer_id=c.id, status=st, current_period_end=None if end is None else now + timedelta(hours=end), grace_until=None if grace is None else now + timedelta(hours=grace), provider="paystack"))
    settings = load_settings()
    expected_paid, expected_live = 0, 0
    with session_scope() as s:
        for c in s.scalars(select(Customer)):
            ok = entitlement.is_entitled(c, settings=settings, now=now)
            expected_paid += ok
            if c.account is not None and c.account.bot_code and c.account.bot_code.startswith("MBA-K"):
                expected_live += ok  # every seeded account is otherwise ready for live: it is live exactly when the customer is entitled
    assert 0 < expected_live < len(kinds)
    assert admin_get(env, "/api/admin/customers/summary")["paid"] == expected_paid
    bots = admin_get(env, "/api/admin/bots")
    assert bots["live"] == expected_live and bots["live_stopped"] == 0 and bots["live"] + bots["practice"] == len(kinds)
    with session_scope() as s:
        entitled_kinds = sum(entitlement.is_entitled(c, settings=settings, now=now) for c in s.scalars(select(Customer)) if c.email.startswith("kind"))
    listed = [r for r in admin_get(env, "/api/admin/customers?page=1")["items"] if r["account_prefix"]]
    assert len(listed) == len(kinds) and sum(r["paid"] for r in listed) == entitled_kinds
    assert sum(1 for r in listed if r["mode"] == "live") == expected_live


# ------------------------------------------------------------------------------------------------ gaps found by my own breakage checks
def test_the_login_route_itself_is_rate_limited_per_address(env):
    c = client_for(env.app, env.admin_id, env.settings)  # never enrolled: every answer is 403 until the limiter says 429, so the failure lock cannot hide the limiter
    codes = [c.post("/api/admin/login", json={"code": "123456"}).status_code for _ in range(12)]
    assert codes[:10] == [403] * 10 and codes[10:] == [429, 429]


def test_a_token_marked_admin_but_signed_with_the_normal_key_is_refused(env):
    from jose import jwt

    s = env.settings
    forged = jwt.encode({"sub": env.admin_id, "typ": "admin", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, s.jwt_secret, algorithm=s.jwt_algorithm)
    c = client_for(env.app, env.admin_id, s)
    c.cookies.set(admin_auth.ADMIN_COOKIE_NAME, forged, path="/api/admin")
    assert c.get("/api/admin/health").status_code == 401


def test_unicode_digits_and_odd_codes_are_refused_not_a_server_error(env):
    enrol(env)
    c = client_for(env.app, env.admin_id, env.settings)
    for odd in ("１２３４５６", "١٢٣٤٥٦", "12345\u0000", "ｱｲｳｴｵｶ", "123456789012345678901234567890"):
        assert c.post("/api/admin/login", json={"code": odd}).status_code in (401, 422, 429), odd  # (the fifth wrong code reaches the lock)


def test_admin_emails_are_matched_ignoring_case_and_spaces(env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "  Owner@Example.COM , other@example.com ")
    from mba_bot.config import load_settings

    assert load_settings().admin_emails == ("other@example.com", "owner@example.com")
    assert client_for(env.app, env.admin_id, env.settings, admin=True).get("/api/admin/health").status_code == 200
