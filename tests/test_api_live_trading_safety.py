"""The safety-critical surface: PUT /api/account/credentials and
POST /api/account/enable-live-trading, plus proof that neither the
Paystack webhook nor trial expiry can independently put an account into
LIVE mode. Same isolated-app-per-module pattern as test_api_e2e.py.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_api_live_safety.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.routers.billing import apply_payment_event  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, Trade  # noqa: E402


def _mark_paid(email: str) -> None:
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        customer.has_paid = True


def _make_eligible(email: str) -> None:
    """Fast-forwards trial_ends_at into the past - the HTTP-layer
    equivalent of the trial simply running out, without waiting 7 days."""
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        customer.account.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)


def _open_a_trade(email: str) -> None:
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        session.add(
            Trade(
                account_id=customer.account.id,
                side="long",
                is_paper=True,
                entry_price=100.0,
                quantity=0.01,
                stop_loss_price=95.0,
                status="open",
            )
        )


def _signup_with_account(client: TestClient, email: str) -> dict:
    client.post(
        "/api/auth/signup",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "accept_terms": True,
            "acknowledge_privacy": True,
        },
    )
    created = client.post(
        "/api/account",
        json={
            "pair": "BTC/USDT",
            "market_type": "spot",
            "notification_channel": "telegram",
            "api_key": "some-key",
            "api_secret": "some-secret",
        },
    )
    assert created.status_code == 201
    return created.json()


def _retag_mainnet(client: TestClient) -> dict:
    response = client.put(
        "/api/account/credentials",
        json={"api_key": "real-key", "api_secret": "real-secret", "environment": "mainnet"},
    )
    assert response.status_code == 200
    return response.json()


# --- enable-live-trading: every rejection path ---


def test_enable_live_trading_rejected_when_unpaid():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "unpaid-enable@example.com")
        _retag_mainnet(client)
        _make_eligible("unpaid-enable@example.com")
        response = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert response.status_code == 409


def test_enable_live_trading_rejected_when_not_eligible():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "noteligible@example.com")
        _mark_paid("noteligible@example.com")
        _retag_mainnet(client)
        # Trial is still running, no bypass accepted.
        response = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert response.status_code == 409


def test_enable_live_trading_rejected_without_mainnet_credential():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "notagged@example.com")
        _mark_paid("notagged@example.com")
        _make_eligible("notagged@example.com")
        # Credential defaults to testnet at signup - never retagged.
        response = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert response.status_code == 409
        assert "mainnet" in response.json()["detail"].lower()


def test_enable_live_trading_rejected_with_open_trade():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "openenable@example.com")
        _mark_paid("openenable@example.com")
        _retag_mainnet(client)
        _make_eligible("openenable@example.com")
        _open_a_trade("openenable@example.com")
        response = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert response.status_code == 409
        assert "open trade" in response.json()["detail"].lower()


def test_enable_live_trading_succeeds_when_every_condition_is_met():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "allgood@example.com")
        _mark_paid("allgood@example.com")
        _retag_mainnet(client)
        _make_eligible("allgood@example.com")

        before = client.get("/api/account").json()
        assert before["mode"] == "paper"

        response = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert response.status_code == 200
        assert response.json()["mode"] == "live"
        assert response.json()["live_trading_enabled_at"] is not None


def test_enable_live_trading_is_idempotent_not_repeatable():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "already@example.com")
        _mark_paid("already@example.com")
        _retag_mainnet(client)
        _make_eligible("already@example.com")
        client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        again = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert again.status_code == 409


# --- credential updates: open-trade guard + auto-disable on downgrade ---


def test_update_credentials_rejected_with_open_trade():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "credopen@example.com")
        _open_a_trade("credopen@example.com")
        response = client.put(
            "/api/account/credentials",
            json={"api_key": "new-key", "api_secret": "new-secret", "environment": "mainnet"},
        )
        assert response.status_code == 409
        assert "open trade" in response.json()["detail"].lower()


def test_downgrading_credential_to_testnet_disables_live_trading():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "downgrade@example.com")
        _mark_paid("downgrade@example.com")
        _retag_mainnet(client)
        _make_eligible("downgrade@example.com")
        enabled = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert enabled.json()["mode"] == "live"

        downgraded = client.put(
            "/api/account/credentials",
            json={"api_key": "test-key", "api_secret": "test-secret", "environment": "testnet"},
        )
        assert downgraded.status_code == 200
        assert downgraded.json()["live_trading_enabled_at"] is None
        assert downgraded.json()["mode"] == "paper"


def test_unsupported_credential_environment_is_rejected():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "badenv@example.com")
        response = client.put(
            "/api/account/credentials",
            json={"api_key": "k", "api_secret": "s", "environment": "staging"},
        )
        assert response.status_code == 422


def test_account_creation_rejects_unsupported_environment():
    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/api/auth/signup",
            json={
                "email": "badcreate@example.com",
                "password": "correct-horse-battery",
                "accept_terms": True,
                "acknowledge_privacy": True,
            },
        )
        response = client.post(
            "/api/account",
            json={
                "pair": "BTC/USDT",
                "market_type": "spot",
                "notification_channel": "telegram",
                "api_key": "k",
                "api_secret": "s",
                "environment": "staging",
            },
        )
        assert response.status_code == 422


def test_account_creation_defaults_credential_to_testnet():
    with TestClient(app, base_url="https://testserver") as client:
        body = _signup_with_account(client, "defaultenv@example.com")
        assert body["credential_environment"] == "testnet"
        assert body["has_mainnet_credential"] is False


# --- nothing except enable-live-trading can ever flip the switch ---


def test_trial_expiry_alone_never_reaches_live_even_when_paid_and_mainnet_tagged():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "expiryonly@example.com")
        _mark_paid("expiryonly@example.com")
        _retag_mainnet(client)
        _make_eligible("expiryonly@example.com")
        # Eligible, paid, mainnet-tagged - everything except the one
        # dedicated enable-live-trading call. Must still be paper.
        response = client.get("/api/account")
        assert response.json()["mode"] == "paper"


def test_payment_webhook_never_sets_live_trading_fields():
    with TestClient(app, base_url="https://testserver") as client:
        body = _signup_with_account(client, "webhookonly@example.com")
        customer_id = None
        with session_scope() as session:
            customer = session.query(Customer).filter(Customer.email == "webhookonly@example.com").one()
            customer_id = customer.id

    # Call the webhook's own event-handling function directly, same as
    # test_api_billing.py does - this is a real Paystack charge.success
    # event, not a stand-in.
    with session_scope() as session:
        apply_payment_event(
            session,
            {
                "event": "charge.success",
                "data": {"status": "success", "reference": "ref_123", "metadata": {"customer_id": customer_id}},
            },
        )

    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == "webhookonly@example.com").one()
        assert customer.has_paid is True
        assert customer.account.live_trading_enabled_at is None
        assert customer.account.credential.environment == "testnet"  # untouched by the webhook
