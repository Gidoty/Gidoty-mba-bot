"""B6a-2: GET /api/account/binance-settings, the read-only "what Binance has for your pair" line, and the approved leverage help wired into the dashboard.

PROVES (fake ccxt client, scratch SQLite): practice and Spot accounts get a plain not-applicable text and no exchange client is even built; a live Futures account reads leverage and margin mode through
exchange.fetch_symbol_leverage_and_margin_mode and gets one of a few fixed texts; no key, secret or exchange error text ever reaches the response; the answer is cached per account (60 s, 10 s for a
failure) and a request that arrives while another is being answered makes no exchange call; the client is closed and only a read is made; the customer text contains nothing marked UNVERIFIED.
DOES NOT PROVE: anything about Binance's real reply, or the dialog in a browser (the accessibility attributes are checked in the source only)."""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_binance_settings.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot import exchange  # noqa: E402
from mba_bot import leverage as lv  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.routers import account as account_router  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SECRET_KEY, SECRET_SECRET = "real-key-ABC123", "real-secret-XYZ789"
_n = iter(range(1, 9999))


class FakeClient:
    def __init__(self, reply=None, error: Exception | None = None):
        self.reply, self.error, self.calls, self.closed = reply, error, [], False

    async def fetch_leverage(self, symbol):
        self.calls.append(("fetch_leverage", symbol))
        if self.error:
            raise self.error
        return self.reply

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fresh_cache():
    account_router._bs_cache.clear()
    account_router._bs_pending.clear()
    yield
    account_router._bs_cache.clear()
    account_router._bs_pending.clear()


def install(monkeypatch, client):
    built = []

    def build(**kwargs):
        built.append(kwargs)
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", build)
    return built


def signup(client, market="futures") -> str:
    email = f"bs{next(_n)}@example.com"
    client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True})
    assert client.post("/api/account", json={"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "api_key": "k", "api_secret": "s"}).status_code == 201
    if market == "futures":
        assert client.patch("/api/account", json={"market_type": "futures"}).status_code == 200
    return email


def go_live(client, email):
    with session_scope() as s:
        s.query(Customer).filter(Customer.email == email).one().has_paid = True
    assert client.put("/api/account/credentials", json={"api_key": SECRET_KEY, "api_secret": SECRET_SECRET, "environment": "mainnet"}).status_code == 200
    with session_scope() as s:
        s.query(Customer).filter(Customer.email == email).one().account.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
    assert client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True}).status_code == 200
    assert client.patch("/api/account", json={"allocated_capital": 500}).status_code == 200
    started = client.post("/api/account/start-bot", json={"confirm": True})
    assert started.status_code == 200 and started.json()["mode"] == "live"


def live(client, market="futures") -> str:
    email = signup(client, market)
    go_live(client, email)
    return email


GOOD = {"symbol": "BTC/USDT:USDT", "marginMode": "isolated", "longLeverage": 5, "shortLeverage": 5}


def test_it_needs_a_login():
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/account/binance-settings").status_code in (401, 403)


def test_a_practice_account_gets_a_plain_text_and_no_client_is_built(monkeypatch):
    built = install(monkeypatch, FakeClient(GOOD))
    with TestClient(app, base_url="https://testserver") as client:
        signup(client)
        r = client.get("/api/account/binance-settings")
        assert r.status_code == 200 and r.json() == {"status": "not_applicable", "pair": None, "leverage": None, "margin_mode": None, "text": lv.TXT_PAPER}
    assert built == []


def test_a_live_spot_account_gets_a_plain_text_and_no_client_is_built(monkeypatch):
    built = install(monkeypatch, FakeClient(GOOD))
    with TestClient(app, base_url="https://testserver") as client:
        live(client, market="spot")
        built.clear()
        r = client.get("/api/account/binance-settings")
        assert r.json()["status"] == "not_applicable" and r.json()["text"] == lv.TXT_SPOT
    assert built == []


@pytest.mark.parametrize("reply,text,mode", [
    (GOOD, "Binance for BTC/USDT: 5x, isolated", "isolated"),
    ({**GOOD, "marginMode": "cross", "longLeverage": 20}, "Binance for BTC/USDT: 20x, cross", "cross"),
    ({"longLeverage": 3}, "Binance for BTC/USDT: 3x, margin mode could not be read", None),
    ({**GOOD, "longLeverage": "10"}, "Binance for BTC/USDT: 10x, isolated", "isolated"),
])
def test_a_live_futures_account_reads_leverage_and_margin_mode(monkeypatch, reply, text, mode):
    fake = FakeClient(reply)
    built = install(monkeypatch, fake)
    with TestClient(app, base_url="https://testserver") as client:
        live(client)
        built.clear()
        r = client.get("/api/account/binance-settings")
        body = r.json()
        assert r.status_code == 200 and body["status"] == "ok" and body["text"] == text and body["margin_mode"] == mode and body["pair"] == "BTC/USDT"
    assert fake.calls == [("fetch_leverage", "BTC/USDT:USDT")]  # one read, the same call the trader makes
    assert fake.closed is True and len(built) == 1 and built[0]["sandbox"] is False and built[0]["market_type"] == "futures"


@pytest.mark.parametrize("fake", [
    FakeClient(None),
    FakeClient({"marginMode": "isolated"}),
    FakeClient({"longLeverage": 0, "marginMode": "isolated"}),
    FakeClient({"longLeverage": True}),
    FakeClient(error=RuntimeError(f"https://fapi.binance.com/fapi/v1/symbolConfig?signature=abcdef&apiKey={SECRET_KEY} {SECRET_SECRET}")),
])
def test_a_failure_is_one_fixed_text_and_nothing_leaks(monkeypatch, fake):
    install(monkeypatch, fake)
    with TestClient(app, base_url="https://testserver") as client:
        live(client)
        r = client.get("/api/account/binance-settings")
        blob = r.text
        assert r.status_code == 200 and r.json()["status"] == "unavailable" and r.json()["text"] == lv.TXT_FAILED
        for secret in (SECRET_KEY, SECRET_SECRET, "signature", "apiKey", "fapi.binance.com", "RuntimeError", "Traceback"):
            assert secret not in blob
    assert fake.closed is True


def test_the_answer_is_cached_per_account(monkeypatch):
    fake = FakeClient(GOOD)
    install(monkeypatch, fake)
    now = [1000.0]
    monkeypatch.setattr(account_router, "_bs_now", lambda: now[0])
    with TestClient(app, base_url="https://testserver") as a, TestClient(app, base_url="https://testserver") as b:
        live(a)
        live(b)
        for _ in range(5):  # a loop of requests makes one exchange call
            assert a.get("/api/account/binance-settings").json()["status"] == "ok"
        assert len(fake.calls) == 1
        assert b.get("/api/account/binance-settings").json()["status"] == "ok" and len(fake.calls) == 2  # another account has its own entry
        now[0] += 59.0
        a.get("/api/account/binance-settings")
        assert len(fake.calls) == 2  # still inside 60 seconds
        now[0] += 2.0
        a.get("/api/account/binance-settings")
        assert len(fake.calls) == 3  # after 60 seconds a fresh read


def test_a_failed_read_is_cached_for_ten_seconds_only(monkeypatch):
    fake = FakeClient(error=RuntimeError("down"))
    install(monkeypatch, fake)
    now = [1000.0]
    monkeypatch.setattr(account_router, "_bs_now", lambda: now[0])
    with TestClient(app, base_url="https://testserver") as client:
        live(client)
        for _ in range(4):
            assert client.get("/api/account/binance-settings").json()["text"] == lv.TXT_FAILED
        assert len(fake.calls) == 1
        now[0] += 11.0
        client.get("/api/account/binance-settings")
        assert len(fake.calls) == 2


def test_a_request_during_another_one_makes_no_exchange_call(monkeypatch):
    fake = FakeClient(GOOD)
    built = install(monkeypatch, fake)
    now = [1000.0]
    monkeypatch.setattr(account_router, "_bs_now", lambda: now[0])
    with TestClient(app, base_url="https://testserver") as client:
        live(client)
        account_id = client.get("/api/account").json()["id"]
        built.clear()
        account_router._bs_pending[account_id] = now[0] + 5.0  # another request for this account is still being answered
        r = client.get("/api/account/binance-settings")
        assert r.json()["text"] == lv.TXT_BUSY and r.json()["status"] == "unavailable"
        assert built == [] and fake.calls == []
        now[0] += 6.0  # the marker has expired: a normal read
        assert client.get("/api/account/binance-settings").json()["status"] == "ok"


def test_the_pending_marker_is_cleared_even_when_the_read_fails(monkeypatch):
    install(monkeypatch, FakeClient(error=RuntimeError("down")))
    with TestClient(app, base_url="https://testserver") as client:
        live(client)
        client.get("/api/account/binance-settings")
        assert account_router._bs_pending == {}


def test_a_new_pair_is_not_served_from_the_old_pairs_cache(monkeypatch):
    fake = FakeClient(GOOD)
    install(monkeypatch, fake)
    with TestClient(app, base_url="https://testserver") as client:
        live(client)
        assert client.get("/api/account/binance-settings").json()["pair"] == "BTC/USDT"
        assert client.patch("/api/account", json={"pair": "ETH/USDT"}).status_code == 200
        r = client.get("/api/account/binance-settings")
        assert r.json()["pair"] == "ETH/USDT" and len(fake.calls) == 2 and fake.calls[1] == ("fetch_leverage", "ETH/USDT:USDT")


def test_it_only_reads(monkeypatch):
    fake = FakeClient(GOOD)
    install(monkeypatch, fake)
    with TestClient(app, base_url="https://testserver") as client:
        live(client)
        assert client.post("/api/account/binance-settings", json={}).status_code == 405
        assert client.put("/api/account/binance-settings", json={}).status_code == 405
        client.get("/api/account/binance-settings")
    assert {c[0] for c in fake.calls} == {"fetch_leverage"}
    src = (ROOT / "src/mba_bot/api/routers/account.py").read_text()
    body = src[src.index("async def get_binance_settings"):src.index('@router.get("/risk-limits")')]
    assert not re.search(r"create_order|cancel_order|set_leverage|set_margin_mode|setLeverage|setMarginMode|edit_order", body)


# ------------------------------------------------------------------ the help wired into the dashboard
def test_the_customer_text_has_nothing_marked_unverified_and_the_messages_agree():
    doc = (ROOT / "docs/customer/leverage_help.md").read_text()
    start = doc.index("## 1. ")
    end = doc.index("## 3. ")
    assert "UNVERIFIED" not in doc[start:end] and "Margin Mode control" not in doc[start:end]
    assert "open the Futures page for the pair on Binance and look for the Leverage and Margin Mode settings" in doc[start:end]
    assert "UNVERIFIED" not in lv.MSG_NOT_ISOLATED and "Margin Mode control" not in lv.MSG_NOT_ISOLATED and "Margin Mode settings" in lv.MSG_NOT_ISOLATED
    assert lv.MSG_NOT_ISOLATED in doc
    for text in (lv.TXT_PAPER, lv.TXT_SPOT, lv.TXT_FAILED, lv.TXT_BUSY):
        assert text in doc
    for example in ("Binance for {pair}: {n}x, isolated", "Binance for {pair}: {n}x, cross", "Binance for {pair}: {n}x, margin mode could not be read"):
        assert example in doc
    assert lv.describe("BTC/USDT", 5, "isolated") == "Binance for BTC/USDT: 5x, isolated" and lv.describe("BTC/USDT", 0, "isolated") is None and lv.describe("BTC/USDT", float("nan"), "isolated") is None
    reviewer = doc[doc.index("## 5. For the reviewer"):]
    assert "UNVERIFIED" in reviewer and "cannot be changed while a position" not in doc[start:end] and "changed while a position is open" in reviewer


def test_the_dialog_is_keyboard_and_screen_reader_friendly_and_readable_on_a_phone():
    src = (ROOT / "frontend/src/components/InfoHelp.jsx").read_text()
    for needle in ('role="dialog"', 'aria-modal="true"', "aria-labelledby", 'aria-haspopup="dialog"', "aria-expanded", "aria-label={`More information:", '"Escape"', '"Tab"', "overflow-y-auto", "max-h-[90vh]",
                   "text-base", "h-11 w-11", "triggerRef.current?.focus()", "document.body.style.overflow", "onMouseDown"):
        assert needle in src, needle
    assert src.count("Close") >= 2  # a Close button at the top and at the bottom
    settings = (ROOT / "frontend/src/pages/BotSettings.jsx").read_text()
    assert "<InfoHelp" in settings and "LEVERAGE_HELP" in settings and "<BinanceSettingsLine" in settings and 'htmlFor="max-leverage"' in settings
    line = (ROOT / "frontend/src/components/BinanceSettingsLine.jsx").read_text()
    assert "MARGIN_MODE_HELP" in line and "/account/binance-settings" in line and 'aria-live="polite"' in line and "Check again" in line
    assert "err.message" not in line and "e.message" not in line  # a failure never shows exchange or error text


def test_an_error_while_building_the_client_is_also_one_fixed_text(monkeypatch):
    def build(**kwargs):
        raise RuntimeError(f"bad key {SECRET_KEY} secret {SECRET_SECRET} https://fapi.binance.com/?signature=abc")

    monkeypatch.setattr(exchange, "build_exchange_client", build)
    with TestClient(app, base_url="https://testserver") as client:
        live(client)
        r = client.get("/api/account/binance-settings")
        assert r.status_code == 200 and r.json()["text"] == lv.TXT_FAILED and r.json()["status"] == "unavailable"
        for secret in (SECRET_KEY, SECRET_SECRET, "signature", "fapi.binance.com", "bad key"):
            assert secret not in r.text
        assert account_router._bs_pending == {}
