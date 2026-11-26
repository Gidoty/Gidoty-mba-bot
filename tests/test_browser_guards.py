"""Browser guards (docs/security/browser_guards.md): the cross-site request check and the security headers, both OFF by default, plus a map of the other security items to the tests that cover them.

PROVES: with both switches off nothing changes; the check refuses a foreign or null Origin and a cross-site fetch on every unsafe method, lets the frontend's own origin (any case, with or without a trailing slash),
same-site fetches and clients with no browser headers through, never looks at GET or the preflight, and never touches the three provider callbacks; the headers appear on every answer including errors and refusals,
HSTS only on https, no-store only on /api, and the docs keep their own policy; the docs warning; each other security item (login throttle and rate limits, the Telegram secret token, the docs switch, the
single-instance lock) still has its named tests. DOES NOT PROVE anything about Nginx, Cloudflare or a real browser."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import Response
from fastapi.testclient import TestClient

from mba_bot.api import app as appmod

FRONTEND = "https://app.example.test"
FLAGS = ("CSRF_ORIGIN_CHECK", "SECURITY_HEADERS", "DISABLE_API_DOCS", "RATE_LIMIT_ENABLED", "LOGIN_THROTTLE_ENABLED", "TELEGRAM_WEBHOOK_SECRET", "API_SINGLE_INSTANCE_LOCK")


@pytest.fixture()
def make_app(monkeypatch, tmp_path):
    def build(**env):
        for name in FLAGS:
            monkeypatch.delenv(name, raising=False)
        base = {"DATABASE_URL": f"sqlite:///{tmp_path}/g.db", "ENCRYPTION_KEY": Fernet.generate_key().decode(), "JWT_SECRET": "test-jwt-secret", "COOKIE_SECURE": "false",
                "POLL_INTERVAL_SECONDS": "3600", "FRONTEND_ORIGIN": FRONTEND, "TELEGRAM_BOT_USERNAME": "mba_test_bot", "MBA_BOT_LOCK_FILE": str(tmp_path / "g.lock")}
        base.update({k: str(v) for k, v in env.items()})
        for k, v in base.items():
            monkeypatch.setenv(k, v)

        async def idle(settings):
            await asyncio.sleep(3600)

        monkeypatch.setattr(appmod, "run_forever", idle)
        return TestClient(appmod.create_app(), base_url="https://testserver")
    return build


UNSAFE = ("POST", "PUT", "PATCH", "DELETE")


# ---------------------------------------------------------------- the cross-site check
def test_with_the_switch_off_a_foreign_origin_is_not_checked(make_app):
    with make_app() as c:
        assert c.post("/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 204


@pytest.mark.parametrize("method", UNSAFE)
@pytest.mark.parametrize("origin", ["https://evil.example", "null", "https://app.example.test.evil.example", "http://app.example.test", "https://app.example.test:8443"])
def test_a_foreign_or_null_origin_is_refused_on_every_unsafe_method(make_app, method, origin):
    with make_app(CSRF_ORIGIN_CHECK="1") as c:
        r = c.request(method, "/api/auth/logout", headers={"Origin": origin})
        assert r.status_code == 403 and r.json() == {"detail": "this request came from another site and was refused"}
        assert "set-cookie" not in r.headers


@pytest.mark.parametrize("origin", [FRONTEND, FRONTEND + "/", "HTTPS://APP.EXAMPLE.TEST"])
def test_the_frontends_own_origin_passes(make_app, origin):
    with make_app(CSRF_ORIGIN_CHECK="1") as c:
        assert c.post("/api/auth/logout", headers={"Origin": origin}).status_code == 204


def test_without_an_origin_only_a_cross_site_fetch_is_refused(make_app):
    with make_app(CSRF_ORIGIN_CHECK="1") as c:
        assert c.post("/api/auth/logout", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert c.post("/api/auth/logout", headers={"Sec-Fetch-Site": "Cross-Site"}).status_code == 403
        for ok in ("same-origin", "same-site", "none"):
            assert c.post("/api/auth/logout", headers={"Sec-Fetch-Site": ok}).status_code == 204, ok
        assert c.post("/api/auth/logout").status_code == 204  # a command-line client or a server: no browser headers


def test_reads_and_the_preflight_are_never_checked(make_app):
    with make_app(CSRF_ORIGIN_CHECK="1") as c:
        evil = {"Origin": "https://evil.example"}
        assert c.get("/api/auth/signups", headers=evil).status_code == 200  # (the health path is exempt anyway, so a different read is used)
        assert c.get("/api/auth/me", headers=evil).status_code == 401  # reaches the route: refused for having no session, not for its origin
        pre = c.options("/api/auth/logout", headers={**evil, "Access-Control-Request-Method": "POST"})
        assert pre.status_code in (200, 400) and pre.headers.get("access-control-allow-origin") != "https://evil.example"


def test_the_provider_callbacks_are_not_checked_here(make_app):
    with make_app(CSRF_ORIGIN_CHECK="1") as c:
        evil = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
        r = c.post("/api/telegram/webhook", json={}, headers=evil)
        assert r.status_code == 200 and r.json() == {"ok": True}
        r2 = c.post("/api/billing/webhook", content=b"{}", headers=evil)
        assert r2.json().get("detail") != "this request came from another site and was refused"
        r3 = c.post("/api/billing/nowpayments/ipn", content=b"{}", headers=evil)
        assert r3.json().get("detail") != "this request came from another site and was refused"


def test_a_refused_request_never_reaches_the_route_and_a_good_one_does(make_app):
    c = make_app(CSRF_ORIGIN_CHECK="1")
    seen = []

    @c.app.post("/api/_probe_write")
    def probe():
        seen.append(1)
        return {"ok": True}

    with c:
        assert c.post("/api/_probe_write", headers={"Origin": "https://evil.example"}).status_code == 403 and seen == []
        assert c.post("/api/_probe_write", headers={"Origin": FRONTEND}).status_code == 200 and seen == [1]


# ---------------------------------------------------------------- headers
HEADERS = ("x-content-type-options", "referrer-policy", "x-frame-options", "content-security-policy")


def test_with_the_switch_off_no_header_is_added(make_app):
    with make_app() as c:
        r = c.get("/api/health")
        assert not any(h in {k.lower() for k in r.headers} for h in HEADERS + ("strict-transport-security", "cache-control"))


def test_with_the_switch_on_every_answer_carries_the_headers_errors_and_refusals_too(make_app):
    with make_app(SECURITY_HEADERS="1", CSRF_ORIGIN_CHECK="1") as c:
        answers = [c.get("/api/health"), c.get("/api/nope"), c.post("/api/auth/login", json={}), c.post("/api/auth/logout", headers={"Origin": "https://evil.example"})]
        assert [a.status_code for a in answers] == [200, 404, 422, 403]
        for r in answers:
            assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"
            assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'" and r.headers["cache-control"] == "no-store"
            assert r.headers["strict-transport-security"].startswith("max-age=")


def test_hsts_only_on_https_and_no_store_only_on_the_api(make_app, tmp_path):
    with make_app(SECURITY_HEADERS="1") as c:
        plain = TestClient(c.app, base_url="http://testserver")
        r = plain.get("/api/health")
        assert "strict-transport-security" not in {k.lower() for k in r.headers} and r.headers["x-content-type-options"] == "nosniff"
        assert plain.get("/docs").headers.get("cache-control") != "no-store"


def test_the_interactive_docs_keep_their_own_policy(make_app):
    with make_app(SECURITY_HEADERS="1") as c:
        for path in ("/docs", "/openapi.json"):
            r = c.get(path)
            assert r.status_code == 200 and "content-security-policy" not in {k.lower() for k in r.headers}, path
            assert r.headers["x-content-type-options"] == "nosniff"


def test_a_header_the_route_already_set_is_not_overwritten(make_app):
    c = make_app(SECURITY_HEADERS="1")

    @c.app.get("/api/_probe")
    def probe(response: Response):
        response.headers["Cache-Control"] = "private, max-age=5"
        return {"ok": True}

    with c:
        r = c.get("/api/_probe")
        assert r.status_code == 200 and r.headers["cache-control"] == "private, max-age=5"


# ---------------------------------------------------------------- docs and the rest of the planning list
def test_the_docs_are_served_by_default_and_a_production_looking_deployment_is_warned(make_app, caplog):
    caplog.set_level(logging.WARNING)
    with make_app() as c:
        assert c.get("/docs").status_code == 200
    assert "API docs are being served" in caplog.text
    caplog.clear()
    with make_app(DISABLE_API_DOCS="1") as c:
        assert c.get("/docs").status_code == 404
    assert "API docs are being served" not in caplog.text
    caplog.clear()
    with make_app(FRONTEND_ORIGIN="http://localhost:5173") as c:
        assert c.get("/docs").status_code == 200
    assert "API docs are being served" not in caplog.text


COVERED = {
    "login throttle": ["test_login_throttle_counts_failures_per_address_and_email_and_expires", "test_login_throttle_locks_after_repeated_failures_even_for_the_right_password", "test_a_successful_login_resets_the_count"],
    "rate limits": ["test_sliding_window_blocks_after_the_limit_and_recovers", "test_the_app_limiter_blocks_with_429_and_retry_after_and_keeps_cors_headers", "test_provider_callbacks_and_health_are_never_limited", "test_general_limit_applies_to_other_api_routes"],
    "telegram secret token": ["test_telegram_webhook_without_a_configured_secret_accepts_everything_as_before", "test_telegram_webhook_with_a_secret_requires_the_exact_header", "test_the_header_comparison_is_constant_time"],
    "docs off when switched off": ["test_docs_are_not_served_when_disabled"],
    "single-instance lock": ["test_flag_on_takes_the_lock_while_running_and_releases_it_after", "test_flag_on_refuses_to_start_a_second_copy", "test_flag_off_ignores_the_lock_entirely"],
}


def test_every_other_security_item_still_has_its_named_tests():
    text = (Path(__file__).parent / "test_security_hardening.py").read_text()
    names = set(re.findall(r"^def (test_\w+)", text, re.M))
    missing = {item: [n for n in tests if n not in names] for item, tests in COVERED.items()}
    assert {k: v for k, v in missing.items() if v} == {}


def test_both_switches_default_off(monkeypatch):
    from mba_bot.config import load_settings

    for f in FLAGS:
        monkeypatch.delenv(f, raising=False)
    s = load_settings()
    assert s.csrf_origin_check is False and s.security_headers is False
