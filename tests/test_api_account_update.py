"""PATCH /api/account: pair/market-type switching, the open-trade safety
guard, and the runtime_status field - via the real HTTP API, same pattern
as test_api_e2e.py (its own isolated DB/app import, not shared state).
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_api_account_update.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot.api.app import app  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, Trade  # noqa: E402


def _mark_paid(email: str) -> None:
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        customer.has_paid = True


def _seed_runtime_fields(email: str) -> None:
    """Puts non-null values in the runtime-status columns so a later
    switch's reset-to-None behavior is actually exercised, not just
    trivially true because nothing had run yet."""
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        account = customer.account
        now = datetime.now(timezone.utc)
        account.last_scanned_at = now
        account.last_signal = "none"
        account.last_signal_at = now
        account.last_error = "a previous, now-stale error"
        account.last_error_at = now


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
    signup = client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery"})
    assert signup.status_code == 201
    created = client.post(
        "/api/account",
        json={
            "pair": "BTC/USDT",
            "market_type": "spot",
            "notification_channel": "telegram",
            "api_key": "k",
            "api_secret": "s",
        },
    )
    assert created.status_code == 201
    return created.json()


def test_switch_pair_and_market_without_open_trade_succeeds():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "switcher1@example.com")
        _seed_runtime_fields("switcher1@example.com")

        response = client.patch("/api/account", json={"pair": "ETH/USDT", "market_type": "futures"})
        assert response.status_code == 200
        body = response.json()
        assert body["pair"] == "ETH/USDT"
        assert body["market_type"] == "futures"

        # Stale readings from the old pair/market shouldn't survive the switch.
        assert body["last_scanned_at"] is None
        assert body["last_signal"] is None
        assert body["last_error"] is None


def test_switch_pair_rejected_while_trade_open():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "switcher2@example.com")
        _open_a_trade("switcher2@example.com")

        response = client.patch("/api/account", json={"pair": "ETH/USDT"})
        assert response.status_code == 409
        assert "open trade" in response.json()["detail"].lower()

        # And the pair must be genuinely unchanged, not just the response rejected.
        unchanged = client.get("/api/account").json()
        assert unchanged["pair"] == "BTC/USDT"


def test_switch_market_type_rejected_while_trade_open():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "switcher3@example.com")
        _open_a_trade("switcher3@example.com")

        response = client.patch("/api/account", json={"market_type": "futures"})
        assert response.status_code == 409

        unchanged = client.get("/api/account").json()
        assert unchanged["market_type"] == "spot"


def test_risk_pct_remains_editable_while_trade_open():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "switcher4@example.com")
        _open_a_trade("switcher4@example.com")

        response = client.patch("/api/account", json={"risk_pct": 1.5})
        assert response.status_code == 200
        assert response.json()["risk_pct"] == 1.5


def test_unsupported_pair_is_rejected():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "switcher5@example.com")
        response = client.patch("/api/account", json={"pair": "DOGE/USDT"})
        assert response.status_code == 422


def test_unsupported_market_type_is_rejected():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "switcher6@example.com")
        response = client.patch("/api/account", json={"market_type": "margin"})
        assert response.status_code == 422


def test_unpaid_account_can_switch_pair():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "unpaid-switch@example.com")
        # Deliberately not marking this customer paid - switching pair is
        # account configuration, not something payment gates.
        response = client.patch("/api/account", json={"pair": "BNB/USDT"})
        assert response.status_code == 200
        assert response.json()["pair"] == "BNB/USDT"


def test_paid_account_can_switch_pair():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "paid-switch@example.com")
        _mark_paid("paid-switch@example.com")
        response = client.patch("/api/account", json={"pair": "XRP/USDT"})
        assert response.status_code == 200
        assert response.json()["pair"] == "XRP/USDT"


def test_switching_to_the_same_pair_is_not_blocked_by_an_open_trade():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "noop-switch@example.com")
        _open_a_trade("noop-switch@example.com")
        # pair is already BTC/USDT - this isn't a real change, so it must
        # not trip the open-trade guard the way an actual switch would.
        response = client.patch("/api/account", json={"pair": "BTC/USDT", "risk_pct": 1.2})
        assert response.status_code == 200
        assert response.json()["risk_pct"] == 1.2


def test_runtime_status_defaults_to_active_with_no_error_or_open_trade():
    with TestClient(app, base_url="https://testserver") as client:
        body = _signup_with_account(client, "status1@example.com")
        assert body["runtime_status"] == "active"


def test_runtime_status_reflects_paused():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "status2@example.com")
        response = client.patch("/api/account", json={"is_active": False})
        assert response.json()["runtime_status"] == "paused"


def test_runtime_status_reflects_trade_open():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "status3@example.com")
        _open_a_trade("status3@example.com")
        response = client.get("/api/account")
        assert response.json()["runtime_status"] == "trade_open"


def test_runtime_status_reflects_error():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "status4@example.com")
        with session_scope() as session:
            customer = session.query(Customer).filter(Customer.email == "status4@example.com").one()
            customer.account.last_error = "No usable trading credentials are configured for this account."
        response = client.get("/api/account")
        assert response.json()["runtime_status"] == "error"


def test_paused_status_wins_over_error_and_open_trade():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "status5@example.com")
        _open_a_trade("status5@example.com")
        with session_scope() as session:
            customer = session.query(Customer).filter(Customer.email == "status5@example.com").one()
            customer.account.last_error = "some error"
            customer.account.is_active = False
        response = client.get("/api/account")
        assert response.json()["runtime_status"] == "paused"
