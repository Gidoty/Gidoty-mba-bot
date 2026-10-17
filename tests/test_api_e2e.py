"""End-to-end smoke test of the HTTP API using FastAPI's in-process
TestClient - no real network, no real exchange or Paystack calls.

Env vars are set *before* importing `mba_bot.api.app` so its module-level
`app = create_app()` and the lifespan's `init_db()` both see this test's
settings rather than whatever `.env` happens to be on disk.

Billing-webhook *signature verification* isn't re-tested here (plain
HMAC-SHA512, exercised implicitly by Paystack itself); `test_api_billing.py`
covers the event-handling logic it wraps directly.

The session cookie is Secure (required alongside SameSite=None for the
real cross-domain frontend/backend split), so the client needs an https
base_url or httpx's cookie jar drops it between requests, same as a
real browser would.
"""
from __future__ import annotations

import os
import tempfile

from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_api.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot.api.app import app  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer  # noqa: E402


def _mark_paid(email: str) -> None:
    """Stands in for the Paystack webhook / mark_customer_paid.py in
    tests that need a paid customer without going through either."""
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        customer.has_paid = True


def test_signup_onboarding_telegram_link_and_live_bypass():
    with TestClient(app, base_url="https://testserver") as client:
        signup = client.post("/api/auth/signup", json={"email": "smoke@example.com", "password": "correct-horse-battery"})
        assert signup.status_code == 201
        assert signup.json()["email"] == "smoke@example.com"

        # account endpoints require the session cookie signup just set
        missing = client.get("/api/account")
        assert missing.status_code == 404

        created = client.post(
            "/api/account",
            json={
                "pair": "BTC/USDT",
                "market_type": "spot",
                "notification_channel": "telegram",
                "api_key": "fake-mainnet-key",
                "api_secret": "fake-mainnet-secret",
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["mode"] == "paper"
        assert body["notification_target"] is None
        assert body["bot_code"].startswith("MBA-")

        duplicate = client.post(
            "/api/account",
            json={
                "pair": "ETH/USDT",
                "market_type": "spot",
                "notification_channel": "telegram",
                "api_key": "k",
                "api_secret": "s",
            },
        )
        assert duplicate.status_code == 409

        link = client.get("/api/telegram/link-url")
        assert link.status_code == 200
        token = link.json()["link_url"].rsplit("start=", 1)[1]

        webhook = client.post(
            "/api/telegram/webhook",
            json={"message": {"chat": {"id": 987654321}, "text": f"/start {token}"}},
        )
        assert webhook.status_code == 200

        after_link = client.get("/api/account").json()
        assert after_link["notification_target"] == "987654321"

        # Accepting the bypass is recorded immediately, but has no effect
        # on actual mode until the customer has paid - an unpaid account
        # is a permanent, free paper trial regardless of the bypass.
        bypass = client.post("/api/account/live-bypass", json={"accept": True})
        assert bypass.status_code == 200
        assert bypass.json()["mode"] == "paper"
        assert bypass.json()["live_bypass_accepted_at"] is not None

        again = client.post("/api/account/live-bypass", json={"accept": True})
        assert again.status_code == 409

        # Paying flips the previously-recorded bypass into effect right away.
        _mark_paid("smoke@example.com")
        after_payment = client.get("/api/account")
        assert after_payment.json()["mode"] == "live"

        update = client.patch("/api/account", json={"risk_pct": 1.5})
        assert update.status_code == 200
        assert update.json()["risk_pct"] == 1.5

        client.post("/api/auth/logout")
        locked_out = client.get("/api/account")
        assert locked_out.status_code == 401


def test_telegram_webhook_bare_start_replies_without_crashing():
    with TestClient(app, base_url="https://testserver") as client:
        client.post("/api/auth/signup", json={"email": "barestart@example.com", "password": "correct-horse-battery"})
        client.post(
            "/api/account",
            json={
                "pair": "BTC/USDT",
                "market_type": "spot",
                "notification_channel": "telegram",
                "api_key": "k",
                "api_secret": "s",
            },
        )

        # A plain /start - no token - must not crash and must not link
        # the chat id to anything, unlike the real deep-link flow.
        webhook = client.post("/api/telegram/webhook", json={"message": {"chat": {"id": 111222333}, "text": "/start"}})
        assert webhook.status_code == 200

        account = client.get("/api/account").json()
        assert account["notification_target"] is None


def test_telegram_webhook_invalid_token_replies_without_crashing():
    with TestClient(app, base_url="https://testserver") as client:
        webhook = client.post(
            "/api/telegram/webhook",
            json={"message": {"chat": {"id": 444555666}, "text": "/start not-a-real-token"}},
        )
        assert webhook.status_code == 200


def test_telegram_webhook_ignores_unrelated_messages():
    with TestClient(app, base_url="https://testserver") as client:
        webhook = client.post(
            "/api/telegram/webhook", json={"message": {"chat": {"id": 777888999}, "text": "hello there"}}
        )
        assert webhook.status_code == 200


def test_signup_rejects_duplicate_email():
    with TestClient(app, base_url="https://testserver") as client:
        client.post("/api/auth/signup", json={"email": "dup@example.com", "password": "correct-horse-battery"})
        second = client.post("/api/auth/signup", json={"email": "dup@example.com", "password": "another-password"})
        assert second.status_code == 409


def test_login_with_wrong_password_is_rejected():
    with TestClient(app, base_url="https://testserver") as client:
        client.post("/api/auth/signup", json={"email": "wrongpw@example.com", "password": "correct-horse-battery"})
        client.post("/api/auth/logout")
        bad_login = client.post("/api/auth/login", json={"email": "wrongpw@example.com", "password": "nope"})
        assert bad_login.status_code == 401
