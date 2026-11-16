"""B6a: the customer leverage setting through the real HTTP API and the Telegram webhook (same isolated-app pattern as test_api_account_update.py).

PROVES: empty reads as 3; whole numbers 1 to 5 only (API); never above the global limit; editable with a trade open (new entries only); Spot accepts and ignores it;
Telegram has no leverage command: a "/leverage 5" from a linked chat changes nothing and replies nothing.
DOES NOT PROVE: anything about Binance."""
from __future__ import annotations

import dataclasses
import os
import tempfile

import pytest
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_customer_leverage_api.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.deps import get_settings  # noqa: E402
from mba_bot.api.routers import telegram as telegram_router  # noqa: E402
from mba_bot.config import load_settings  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, Trade  # noqa: E402

_counter = iter(range(10_000, 99_999))


def _signup(client: TestClient, market: str = "futures") -> tuple[str, str]:
    n = next(_counter)
    email = f"lev{n}@example.com"
    assert client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True}).status_code == 201
    created = client.post("/api/account", json={"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "api_key": "k", "api_secret": "s"})
    assert created.status_code == 201
    if market == "futures":
        assert client.patch("/api/account", json={"market_type": "futures"}).status_code == 200
    return email, str(900_000 + n)


def _link_chat(client: TestClient, chat_id: str, replies: list) -> None:  # the normal /start link flow, the only thing the webhook understands
    token = client.get("/api/telegram/link-url").json()["link_url"].rsplit("start=", 1)[1]
    assert client.post("/api/telegram/webhook", json={"message": {"chat": {"id": int(chat_id)}, "text": f"/start {token}"}}).status_code == 200


@pytest.fixture
def replies(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_reply(chat_id, message, *, settings):
        sent.append((chat_id, message))

    monkeypatch.setattr(telegram_router, "_reply", fake_reply)
    return sent


@pytest.fixture
def global_limit():
    def set_limit(value):
        app.dependency_overrides[get_settings] = lambda: dataclasses.replace(load_settings(), futures_max_leverage=float(value))

    yield set_limit
    app.dependency_overrides.pop(get_settings, None)


def test_empty_reads_as_three_and_the_limit_is_five():
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        body = client.get("/api/account").json()
        assert (body["max_leverage"], body["max_leverage_limit"]) == (3, 5)


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_whole_numbers_one_to_five_are_stored(n):
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        r = client.patch("/api/account", json={"max_leverage": n})
        assert r.status_code == 200 and r.json()["max_leverage"] == n and client.get("/api/account").json()["max_leverage"] == n


@pytest.mark.parametrize("bad", [0, 6, -1, 100, 2.5, "3", True, "abc", [3]])
def test_anything_else_is_rejected_and_nothing_changes(bad):
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        assert client.patch("/api/account", json={"max_leverage": 4}).status_code == 200
        assert client.patch("/api/account", json={"max_leverage": bad}).status_code == 422
        assert client.get("/api/account").json()["max_leverage"] == 4


def test_cannot_exceed_the_global_limit(global_limit):
    global_limit(2)
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        body = client.get("/api/account").json()
        assert (body["max_leverage"], body["max_leverage_limit"]) == (2, 2)  # the default 3 is read as 2
        bad = client.patch("/api/account", json={"max_leverage": 3})
        assert bad.status_code == 422 and "1 to 2" in bad.json()["detail"]
        assert client.patch("/api/account", json={"max_leverage": 2}).json()["max_leverage"] == 2


def test_a_stored_number_above_a_lowered_global_limit_is_read_as_the_limit(global_limit):
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client)
        client.patch("/api/account", json={"max_leverage": 5})
        global_limit(2)
        body = client.get("/api/account").json()
        assert (body["max_leverage"], body["max_leverage_limit"]) == (2, 2)


def test_it_can_be_changed_with_a_trade_open_and_the_trade_is_not_touched():
    with TestClient(app, base_url="https://testserver") as client:
        email, _ = _signup(client)
        with session_scope() as s:
            acct = s.query(Customer).filter(Customer.email == email).one().account
            acct_id = acct.id
            s.add(Trade(account_id=acct.id, side="long", is_paper=True, entry_price=100.0, quantity=0.01, stop_loss_price=95.0, status="open"))
        assert client.patch("/api/account", json={"max_leverage": 1}).status_code == 200
        assert client.patch("/api/account", json={"pair": "ETH/USDT"}).status_code == 409  # unrelated guard still applies
        with session_scope() as s:
            t = s.query(Trade).filter(Trade.account_id == acct_id).one()  # (the database file is shared with other API test modules in a full run)
            assert (t.status, t.entry_price, t.quantity, t.stop_loss_price) == ("open", 100.0, 0.01, 95.0)


def test_spot_accepts_the_setting_and_it_has_no_effect_there():
    with TestClient(app, base_url="https://testserver") as client:
        _signup(client, market="spot")
        r = client.patch("/api/account", json={"max_leverage": 2})
        assert r.status_code == 200 and r.json()["market_type"] == "spot"


# ------------------------------------------------------------------ Telegram: there is no leverage command
def test_a_leverage_message_from_a_linked_chat_changes_nothing_and_replies_nothing(replies):
    """The setting is changed on the dashboard only. The webhook understands /start with a link token and nothing else."""
    with TestClient(app, base_url="https://testserver") as client:
        _, chat = _signup(client)
        _link_chat(client, chat, replies)
        assert client.patch("/api/account", json={"max_leverage": 4}).status_code == 200
        replies.clear()
        for text in ("/leverage 5", "/leverage", "/leverage@mba_test_bot 1", "/LEVERAGE 2", "/help", "/help leverage", "leverage 5"):
            r = client.post("/api/telegram/webhook", json={"message": {"chat": {"id": int(chat)}, "text": text}})
            assert r.status_code == 200 and r.json() == {"ok": True}
        assert replies == []  # nothing was sent back
        assert client.get("/api/account").json()["max_leverage"] == 4  # and nothing changed


def test_the_telegram_router_has_no_leverage_code():
    import inspect

    source = inspect.getsource(telegram_router)
    assert "leverage" not in source.lower() and "/help" not in source
