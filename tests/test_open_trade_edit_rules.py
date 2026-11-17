"""What can be changed with a trade open, through the real HTTP API: risk per trade, leverage and trading capital can (they apply to new trades only); pair, market and pair selection mode cannot (409).
This is what the Bot Settings tooltip says (see tests/test_risk_definition_text.py, which ties the sentence to the code)."""
from __future__ import annotations

import os
import tempfile

from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_open_trade_edit_rules.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from mba_bot.api.app import app  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, Trade  # noqa: E402

_n = iter(range(1, 999))


def account_with_open_trade(client: TestClient) -> dict:
    email = f"edit{next(_n)}@example.com"
    client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True})
    assert client.post("/api/account", json={"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "api_key": "k", "api_secret": "s"}).status_code == 201
    assert client.patch("/api/account", json={"market_type": "futures"}).status_code == 200
    with session_scope() as s:
        account = s.query(Customer).filter(Customer.email == email).one().account
        s.add(Trade(account_id=account.id, side="long", is_paper=True, entry_price=100.0, quantity=0.01, stop_loss_price=95.0, status="open", pair="BTC/USDT"))
    return client.get("/api/account").json()


@pytest.mark.parametrize("body,field,value", [
    ({"risk_pct": 1.5}, "risk_pct", 1.5),
    ({"max_leverage": 2}, "max_leverage", 2),
    ({"allocated_capital": 321.0}, "allocated_capital", 321.0),
])
def test_risk_leverage_and_capital_can_be_changed_with_a_trade_open(body, field, value):
    with TestClient(app, base_url="https://testserver") as client:
        account_with_open_trade(client)
        r = client.patch("/api/account", json=body)
        assert r.status_code == 200 and r.json()[field] == value
        assert client.get("/api/account").json()[field] == value


def test_the_open_trade_is_not_touched_by_those_changes():
    with TestClient(app, base_url="https://testserver") as client:
        account = account_with_open_trade(client)
        assert client.patch("/api/account", json={"risk_pct": 1.7, "max_leverage": 1, "allocated_capital": 250.0}).status_code == 200
        with session_scope() as s:
            t = s.query(Trade).filter(Trade.account_id == account["id"]).one()
            assert (t.status, t.entry_price, t.quantity, t.stop_loss_price) == ("open", 100.0, 0.01, 95.0)


@pytest.mark.parametrize("body", [{"pair": "ETH/USDT"}, {"market_type": "spot"}, {"pair_selection_mode": "auto"}])
def test_pair_market_and_pair_selection_mode_cannot_be_changed_with_a_trade_open(body):
    with TestClient(app, base_url="https://testserver") as client:
        before = account_with_open_trade(client)
        r = client.patch("/api/account", json=body)
        assert r.status_code == 409 and "open trade" in r.json()["detail"].lower()
        after = client.get("/api/account").json()
        for key in ("pair", "market_type", "pair_selection_mode"):
            assert after[key] == before[key]


def test_a_request_that_also_changes_a_blocked_field_changes_nothing():
    with TestClient(app, base_url="https://testserver") as client:
        before = account_with_open_trade(client)
        r = client.patch("/api/account", json={"risk_pct": 1.9, "pair": "ETH/USDT"})
        assert r.status_code == 409
        assert client.get("/api/account").json()["risk_pct"] == before["risk_pct"]
