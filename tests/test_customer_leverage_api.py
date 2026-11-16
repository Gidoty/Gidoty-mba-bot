"""B6a: the customer leverage setting through the real HTTP API and the Telegram webhook (same isolated-app pattern as test_api_account_update.py).

PROVES: empty reads as 3; whole numbers 1 to 5 only (API and Telegram); never above the global limit; editable with a trade open (new entries only); Spot accepts and ignores it;
only the chat linked to an account can read or change it; replies are the fixed strings in docs/customer/leverage_help.md.
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


def _link_chat(client: TestClient, chat_id: str, replies: list) -> None:
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


# ------------------------------------------------------------------ Telegram
def test_telegram_shows_and_changes_the_setting(replies):
    with TestClient(app, base_url="https://testserver") as client:
        _, chat = _signup(client)
        _link_chat(client, chat, replies)
        replies.clear()
        post = lambda text: client.post("/api/telegram/webhook", json={"message": {"chat": {"id": int(chat)}, "text": text}})
        post("/leverage")
        assert "Your leverage setting is 3x" in replies[-1][1] and "from 1 to 5" in replies[-1][1] and "never changes your leverage or margin mode" in replies[-1][1]
        post("/leverage 2")
        assert replies[-1][1].startswith("Done. Your leverage setting is now 2x") and client.get("/api/account").json()["max_leverage"] == 2
        post("/leverage@mba_test_bot 4")
        assert client.get("/api/account").json()["max_leverage"] == 4
        post("/LEVERAGE 5")
        assert client.get("/api/account").json()["max_leverage"] == 5


@pytest.mark.parametrize("bad", ["0", "6", "-1", "2.5", "abc", "3x", "٣", "99999999999999999999", "1 2"])
def test_telegram_rejects_anything_but_a_whole_number_in_range(replies, bad):
    with TestClient(app, base_url="https://testserver") as client:
        _, chat = _signup(client)
        _link_chat(client, chat, replies)
        client.patch("/api/account", json={"max_leverage": 4})
        replies.clear()
        client.post("/api/telegram/webhook", json={"message": {"chat": {"id": int(chat)}, "text": f"/leverage {bad}"}})
        assert "Nothing was changed" in replies[-1][1] and client.get("/api/account").json()["max_leverage"] == 4


def test_telegram_cannot_exceed_the_global_limit(replies, global_limit):
    global_limit(2)
    with TestClient(app, base_url="https://testserver") as client:
        _, chat = _signup(client)
        _link_chat(client, chat, replies)
        replies.clear()
        client.post("/api/telegram/webhook", json={"message": {"chat": {"id": int(chat)}, "text": "/leverage 3"}})
        assert "from 1 to 2" in replies[-1][1] and "Nothing was changed" in replies[-1][1] and client.get("/api/account").json()["max_leverage"] == 2


def test_telegram_spot_account_and_unlinked_chat_get_fixed_replies_and_nothing_changes(replies):
    with TestClient(app, base_url="https://testserver") as client:
        _, chat = _signup(client, market="spot")
        _link_chat(client, chat, replies)
        replies.clear()
        client.post("/api/telegram/webhook", json={"message": {"chat": {"id": int(chat)}, "text": "/leverage 1"}})
        assert replies[-1][1].startswith("The leverage setting applies to Futures only")
        client.post("/api/telegram/webhook", json={"message": {"chat": {"id": 424242424}, "text": "/leverage 1"}})
        assert replies[-1] == ("424242424", telegram_router._NOT_LINKED_MESSAGE)
        assert client.get("/api/account").json()["max_leverage"] == 3


def test_a_chat_can_only_change_its_own_account(replies):
    with TestClient(app, base_url="https://testserver") as a, TestClient(app, base_url="https://testserver") as b:
        _, chat_a = _signup(a)
        _link_chat(a, chat_a, replies)
        _, chat_b = _signup(b)
        _link_chat(b, chat_b, replies)
        b.post("/api/telegram/webhook", json={"message": {"chat": {"id": int(chat_b)}, "text": "/leverage 1"}})
        assert b.get("/api/account").json()["max_leverage"] == 1 and a.get("/api/account").json()["max_leverage"] == 3


def test_a_chat_shared_by_two_accounts_is_refused_and_changes_nothing(replies):
    with TestClient(app, base_url="https://testserver") as a, TestClient(app, base_url="https://testserver") as b:
        _, chat = _signup(a)
        _link_chat(a, chat, replies)
        email_b, _ = _signup(b)
        with session_scope() as s:
            s.query(Customer).filter(Customer.email == email_b).one().account.notification_target = chat  # the same chat on a second account
        replies.clear()
        a.post("/api/telegram/webhook", json={"message": {"chat": {"id": int(chat)}, "text": "/leverage 1"}})
        assert replies[-1][1] == telegram_router._NOT_LINKED_MESSAGE
        assert a.get("/api/account").json()["max_leverage"] == 3 and b.get("/api/account").json()["max_leverage"] == 3
