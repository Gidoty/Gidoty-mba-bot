"""Futures pair expansion over the real HTTP API: flag off changes nothing,
flag on adds manual-only Futures pairs with per-capital availability."""
from __future__ import annotations

import os
import tempfile

import pytest
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_pair_expansion_api.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot import pair_catalog  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.config import ALL_KNOWN_PAIRS, FUTURES_EXTRA_PAIRS, SUPPORTED_PAIRS  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, Trade  # noqa: E402
from mba_bot.pair_catalog import PairFacts, REASON_INSUFFICIENT_CAPITAL, REASON_NOT_LISTED  # noqa: E402

_counter = iter(range(10_000))


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv("FUTURES_PAIR_EXPANSION", raising=False)
    calls = []

    async def facts():
        calls.append(1)
        return {p: PairFacts(listed=True, min_notional=5.0) for p in ALL_KNOWN_PAIRS}

    monkeypatch.setattr(pair_catalog, "futures_pair_facts", facts)
    return calls


@pytest.fixture
def fetches(_flag_off):
    return _flag_off


def _flag_on(monkeypatch):
    monkeypatch.setenv("FUTURES_PAIR_EXPANSION", "1")


def _facts(monkeypatch, notionals=None, unlisted=(), fail=False):
    async def facts():
        if fail:
            return None
        return {
            p: PairFacts(listed=p not in unlisted, min_notional=(notionals or {}).get(p, 5.0)) for p in ALL_KNOWN_PAIRS
        }

    monkeypatch.setattr(pair_catalog, "futures_pair_facts", facts)


def _signup(client, pair="BTC/USDT", market="spot", capital=None):
    email = f"pexp{next(_counter)}@example.com"
    r = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True},
    )
    assert r.status_code == 201
    body = {"pair": pair, "market_type": market, "notification_channel": "telegram", "api_key": "k", "api_secret": "s"}
    if capital is not None:
        body["allocated_capital"] = capital
    r = client.post("/api/account", json=body)
    return email, r


def _set_capital(email, value):
    with session_scope() as s:
        s.query(Customer).filter(Customer.email == email).one().account.allocated_capital = value


def _open_trade(email):
    with session_scope() as s:
        acct = s.query(Customer).filter(Customer.email == email).one().account
        s.add(Trade(account_id=acct.id, side="long", is_paper=True, entry_price=1.0, quantity=1.0, stop_loss_price=0.9, status="open"))


def _client():
    return TestClient(app, base_url="https://testserver")


def _by_pair(r):
    return {p["pair"]: p for p in r.json()["pairs"]}


# ------------------------------------------------------------------ flag OFF


def test_flag_off_lists_only_the_original_four_for_both_markets(fetches):
    with _client() as c:
        _signup(c)
        for market in ("spot", "futures"):
            r = c.get("/api/pairs", params={"market_type": market})
            assert r.status_code == 200
            assert [p["pair"] for p in r.json()["pairs"]] == list(SUPPORTED_PAIRS)
            assert all(p["available"] and p["reason"] is None for p in r.json()["pairs"])
    assert fetches == [], "no exchange lookup with the flag off"


@pytest.mark.parametrize("pair", FUTURES_EXTRA_PAIRS)
def test_flag_off_rejects_extra_pairs(pair, fetches):
    with _client() as c:
        _, r = _signup(c, pair=pair, market="futures")
        assert r.status_code == 422
        _signup(c)
        assert c.patch("/api/account", json={"pair": pair, "market_type": "futures"}).status_code == 422
    assert fetches == []


def test_flag_off_original_pairs_still_switch_freely():
    with _client() as c:
        _signup(c)
        assert c.patch("/api/account", json={"pair": "ETH/USDT", "market_type": "futures"}).status_code == 200


# ------------------------------------------------------------------ flag ON


def test_flag_on_futures_lists_ten_pairs_spot_stays_four(monkeypatch):
    _flag_on(monkeypatch)
    with _client() as c:
        _signup(c)
        fut = c.get("/api/pairs", params={"market_type": "futures"})
        assert [p["pair"] for p in fut.json()["pairs"]] == list(SUPPORTED_PAIRS + FUTURES_EXTRA_PAIRS)
        spot = c.get("/api/pairs", params={"market_type": "spot"})
        assert [p["pair"] for p in spot.json()["pairs"]] == list(SUPPORTED_PAIRS)


@pytest.mark.parametrize("pair", FUTURES_EXTRA_PAIRS)
def test_flag_on_extra_pair_accepted_for_futures_rejected_for_spot(pair, monkeypatch):
    _flag_on(monkeypatch)
    with _client() as c:
        _, r = _signup(c, pair=pair, market="futures")
        assert r.status_code == 201 and r.json()["pair"] == pair
        _, r = _signup(c, pair=pair, market="spot")
        assert r.status_code == 422


def test_unaffordable_pairs_are_shown_and_marked_with_the_exact_reason(monkeypatch):
    _flag_on(monkeypatch)
    _facts(monkeypatch, notionals={"BTC/USDT": 100.0, "SOL/USDT": 5.0})
    with _client() as c:
        email, _ = _signup(c)
        _set_capital(email, 20)  # ceiling 20 * 3 = 60 < 100
        pairs = _by_pair(c.get("/api/pairs", params={"market_type": "futures"}))
        assert len(pairs) == 10
        assert pairs["BTC/USDT"] == {"pair": "BTC/USDT", "available": False, "reason": REASON_INSUFFICIENT_CAPITAL}
        assert REASON_INSUFFICIENT_CAPITAL == "Insufficient allocated capital for this market."
        assert pairs["SOL/USDT"]["available"] is True
        assert "$" not in (pairs["BTC/USDT"]["reason"] or ""), "no estimated minimum shown"
        _set_capital(email, 50)
        assert _by_pair(c.get("/api/pairs", params={"market_type": "futures"}))["BTC/USDT"]["available"] is True


def test_unknown_capital_means_available(monkeypatch):
    _flag_on(monkeypatch)
    _facts(monkeypatch, notionals={"BTC/USDT": 1000.0})
    with _client() as c:
        email, _ = _signup(c)
        _set_capital(email, None)
        assert _by_pair(c.get("/api/pairs", params={"market_type": "futures"}))["BTC/USDT"]["available"] is True


def test_pair_not_listed_is_marked_not_hidden(monkeypatch):
    _flag_on(monkeypatch)
    _facts(monkeypatch, unlisted=("SUI/USDT",))
    with _client() as c:
        _signup(c)
        pairs = _by_pair(c.get("/api/pairs", params={"market_type": "futures"}))
        assert pairs["SUI/USDT"] == {"pair": "SUI/USDT", "available": False, "reason": REASON_NOT_LISTED}


def test_catalog_outage_fails_open_for_display(monkeypatch):
    _flag_on(monkeypatch)
    _facts(monkeypatch, fail=True)
    with _client() as c:
        _signup(c)
        r = c.get("/api/pairs", params={"market_type": "futures"})
        assert r.status_code == 200 and len(r.json()["pairs"]) == 10 and all(p["available"] for p in r.json()["pairs"])


def test_patch_to_an_unaffordable_pair_is_refused_with_the_reason(monkeypatch):
    _flag_on(monkeypatch)
    _facts(monkeypatch, notionals={"LINK/USDT": 100.0})
    with _client() as c:
        email, _ = _signup(c, market="futures")
        _set_capital(email, 20)
        r = c.patch("/api/account", json={"pair": "LINK/USDT"})
        assert r.status_code == 422 and r.json()["detail"] == REASON_INSUFFICIENT_CAPITAL
        assert c.get("/api/account").json()["pair"] == "BTC/USDT"
        # Raising capital in the same request makes it selectable.
        r = c.patch("/api/account", json={"pair": "LINK/USDT", "allocated_capital": 50})
        assert r.status_code == 200 and r.json()["pair"] == "LINK/USDT"


def test_risk_edit_is_not_blocked_by_an_already_unaffordable_current_pair(monkeypatch):
    _flag_on(monkeypatch)
    _facts(monkeypatch, notionals={"DOGE/USDT": 5.0})
    with _client() as c:
        email, _ = _signup(c, pair="DOGE/USDT", market="futures")
        _facts(monkeypatch, notionals={"DOGE/USDT": 1000.0})
        _set_capital(email, 20)
        r = c.patch("/api/account", json={"risk_pct": 1.5})
        assert r.status_code == 200 and r.json()["risk_pct"] == 1.5


def test_switching_to_spot_with_an_extra_pair_is_refused(monkeypatch):
    _flag_on(monkeypatch)
    with _client() as c:
        _signup(c, pair="SOL/USDT", market="futures")
        r = c.patch("/api/account", json={"market_type": "spot"})
        assert r.status_code == 422
        assert c.get("/api/account").json()["market_type"] == "futures"
        assert c.patch("/api/account", json={"market_type": "spot", "pair": "BTC/USDT"}).status_code == 200


def test_open_trade_still_blocks_a_pair_change(monkeypatch):
    _flag_on(monkeypatch)
    with _client() as c:
        email, _ = _signup(c, market="futures")
        _open_trade(email)
        assert c.patch("/api/account", json={"pair": "SOL/USDT"}).status_code == 409


def test_pairs_endpoint_requires_auth_and_valid_market():
    with _client() as c:
        assert c.get("/api/pairs", params={"market_type": "futures"}).status_code == 401
        _signup(c)
        assert c.get("/api/pairs", params={"market_type": "margin"}).status_code == 422
