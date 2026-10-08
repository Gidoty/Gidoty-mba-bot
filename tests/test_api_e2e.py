"""End-to-end smoke test of the HTTP API using FastAPI's in-process
TestClient - no real network, no real exchange or Stripe calls.

Env vars are set *before* importing `mba_bot.api.app` so its module-level
`app = create_app()` and the lifespan's `init_db()` both see this test's
settings rather than whatever `.env` happens to be on disk.

Billing-webhook *signature verification* isn't re-tested here (that's
Stripe's own library code); `test_api_billing.py` covers the
event-handling logic it wraps directly.
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


def test_signup_onboarding_telegram_link_and_live_bypass():
    with TestClient(app) as client:
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

        bypass = client.post("/api/account/live-bypass", json={"accept": True})
        assert bypass.status_code == 200
        assert bypass.json()["mode"] == "live"
        assert bypass.json()["live_bypass_accepted_at"] is not None

        again = client.post("/api/account/live-bypass", json={"accept": True})
        assert again.status_code == 409

        update = client.patch("/api/account", json={"risk_pct": 1.5})
        assert update.status_code == 200
        assert update.json()["risk_pct"] == 1.5

        client.post("/api/auth/logout")
        locked_out = client.get("/api/account")
        assert locked_out.status_code == 401


def test_signup_rejects_duplicate_email():
    with TestClient(app) as client:
        client.post("/api/auth/signup", json={"email": "dup@example.com", "password": "correct-horse-battery"})
        second = client.post("/api/auth/signup", json={"email": "dup@example.com", "password": "another-password"})
        assert second.status_code == 409


def test_login_with_wrong_password_is_rejected():
    with TestClient(app) as client:
        client.post("/api/auth/signup", json={"email": "wrongpw@example.com", "password": "correct-horse-battery"})
        client.post("/api/auth/logout")
        bad_login = client.post("/api/auth/login", json={"email": "wrongpw@example.com", "password": "nope"})
        assert bad_login.status_code == 401
