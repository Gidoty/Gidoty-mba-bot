"""GET /api/account/balance, GET /api/account/position, and
POST /api/account/credentials/test - all three are read-only and never
place an order. The real ccxt client is replaced with a fake (same
spirit as test_trader_runtime_status.py's FakeExchangeClient) so these
tests never touch the network, and exercise both the happy path and the
"exchange call failed" path to confirm raw exception text never reaches
the response.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt_async
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_account_live_data.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"
os.environ["TESTNET_SPOT_API_KEY"] = "platform-testnet-key"
os.environ["TESTNET_SPOT_API_SECRET"] = "platform-testnet-secret"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot import exchange  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, Trade  # noqa: E402


class _FakeClient:
    def __init__(self, *, free_balance=None, ticker_last=None, fail_with: Exception | None = None):
        self._free_balance = free_balance or {}
        self._ticker_last = ticker_last
        self._fail_with = fail_with

    async def fetch_balance(self):
        if self._fail_with:
            raise self._fail_with
        return {"free": self._free_balance, "info": {}}

    async def fetch_ticker(self, pair):
        if self._fail_with:
            raise self._fail_with
        return {"last": self._ticker_last}

    async def close(self):
        pass


def _patch_exchange_client(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)


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
            "api_key": "k",
            "api_secret": "s",
        },
    )
    assert created.status_code == 201
    return created.json()


def _mark_paid(email: str) -> None:
    with session_scope() as session:
        session.query(Customer).filter(Customer.email == email).one().has_paid = True


def _make_eligible(email: str) -> None:
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        customer.account.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)


def _go_live(client: TestClient, email: str) -> None:
    _mark_paid(email)
    retag = client.put(
        "/api/account/credentials",
        json={"api_key": "real-key", "api_secret": "real-secret", "environment": "mainnet"},
    )
    assert retag.status_code == 200
    _make_eligible(email)
    enabled = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
    assert enabled.status_code == 200
    # Enabling live trading authorizes going live; it never starts
    # anything by itself - the account only reaches mode="live" once
    # Trading Capital is configured and Start Bot is explicitly pressed.
    capital = client.patch("/api/account", json={"allocated_capital": 500})
    assert capital.status_code == 200
    started = client.post("/api/account/start-bot", json={"confirm": True})
    assert started.status_code == 200
    assert started.json()["mode"] == "live"


# --- GET /api/account/balance ---


def test_balance_rejected_when_not_live():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "balnotlive@example.com")
        response = client.get("/api/account/balance")
        assert response.status_code == 409


def test_balance_returns_live_free_quote_balance(monkeypatch):
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "balok@example.com")
        _go_live(client, "balok@example.com")
        _patch_exchange_client(monkeypatch, _FakeClient(free_balance={"USDT": 1234.5}))

        response = client.get("/api/account/balance")
        assert response.status_code == 200
        body = response.json()
        assert body["quote_currency"] == "USDT"
        assert body["available_balance"] == 1234.5


def test_balance_exchange_failure_returns_safe_message_not_raw_exception(monkeypatch):
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "balfail@example.com")
        _go_live(client, "balfail@example.com")
        _patch_exchange_client(
            monkeypatch, _FakeClient(fail_with=ccxt_async.AuthenticationError("raw-secret-leak-12345"))
        )

        response = client.get("/api/account/balance")
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "raw-secret-leak-12345" not in detail
        assert "rejected this account's API key" in detail


# --- GET /api/account/position ---


def test_position_reports_no_position_when_nothing_open():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "noposition@example.com")
        response = client.get("/api/account/position")
        assert response.status_code == 200
        assert response.json() == {
            "has_position": False,
            "pair": None,
            "side": None,
            "is_paper": None,
            "entry_price": None,
            "current_price": None,
            "quantity": None,
            "stop_loss_price": None,
            "unrealized_pnl": None,
            "opened_at": None,
        }


def _open_a_trade(email: str, *, side: str = "long") -> None:
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        session.add(
            Trade(
                account_id=customer.account.id,
                side=side,
                is_paper=True,
                entry_price=100.0,
                quantity=2.0,
                stop_loss_price=95.0,
                status="open",
            )
        )


def test_position_computes_unrealized_pnl_for_a_long(monkeypatch):
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "longpos@example.com")
        _open_a_trade("longpos@example.com", side="long")
        _patch_exchange_client(monkeypatch, _FakeClient(ticker_last=110.0))

        response = client.get("/api/account/position")
        assert response.status_code == 200
        body = response.json()
        assert body["has_position"] is True
        assert body["side"] == "long"
        assert body["entry_price"] == 100.0
        assert body["current_price"] == 110.0
        assert body["unrealized_pnl"] == 20.0  # (110 - 100) * 2


def test_position_computes_unrealized_pnl_for_a_short(monkeypatch):
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "shortpos@example.com")
        _open_a_trade("shortpos@example.com", side="short")
        _patch_exchange_client(monkeypatch, _FakeClient(ticker_last=90.0))

        response = client.get("/api/account/position")
        assert response.status_code == 200
        body = response.json()
        assert body["unrealized_pnl"] == 20.0  # (100 - 90) * 2


def test_position_degrades_gracefully_when_price_fetch_fails(monkeypatch):
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "pricefail@example.com")
        _open_a_trade("pricefail@example.com")
        _patch_exchange_client(monkeypatch, _FakeClient(fail_with=RuntimeError("network down")))

        response = client.get("/api/account/position")
        assert response.status_code == 200  # never a 5xx just because the ticker call failed
        body = response.json()
        assert body["has_position"] is True
        assert body["current_price"] is None
        assert body["unrealized_pnl"] is None


# --- POST /api/account/credentials/test ---


def test_credentials_test_succeeds(monkeypatch):
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "testconn@example.com")
        _patch_exchange_client(monkeypatch, _FakeClient())

        response = client.post("/api/account/credentials/test")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True


def test_credentials_test_reports_failure_without_leaking_raw_exception(monkeypatch):
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "testconnfail@example.com")
        _patch_exchange_client(
            monkeypatch, _FakeClient(fail_with=ccxt_async.AuthenticationError("raw-secret-leak-67890"))
        )

        response = client.post("/api/account/credentials/test")
        assert response.status_code == 200  # a failed *test* is still a successful request
        body = response.json()
        assert body["ok"] is False
        assert "raw-secret-leak-67890" not in body["message"]
