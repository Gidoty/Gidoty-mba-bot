"""Customer pair list for live multi-position (docs/safety/live_multi_position.md, section "Choosing the extra pairs"). Behind LIVE_MULTI_POSITION (default off).

PROVES: with the switch off a pair list is refused (409) and the new account fields are None (as today); with it on the customer can choose at most (slots - 1) extra pairs, no duplicates, not the main pair,
only pairs the service offers, only with Manual pair selection, and never drop a pair that has an open trade; the list is stored in the owner-set `pair_list` column the bot already reads (live_pairs);
a refused request changes nothing; the open-risk cap is shown read-only and no request can change it.
DOES NOT PROVE: the screens, or Binance."""
from __future__ import annotations

import dataclasses
import os
import tempfile

import pytest
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_live_pair_list.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot import live_multi  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.deps import get_settings  # noqa: E402
from mba_bot.config import load_settings  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, Trade  # noqa: E402
from test_api_live_trading_safety import _signup_with_account  # noqa: E402

_n = iter(range(1, 9999))


@pytest.fixture(autouse=True)
def _clean():
    yield
    app.dependency_overrides.pop(get_settings, None)


def _on(**kw):
    app.dependency_overrides[get_settings] = lambda: dataclasses.replace(load_settings(), live_multi_position=True, live_total_risk_cap_pct=3.0, **kw)


def _client():
    c = TestClient(app, base_url="https://testserver")
    c.__enter__()
    email = f"pl{next(_n)}@example.com"
    _signup_with_account(c, email)
    return c, email


def _patch(c, **body):
    return c.patch("/api/account", json=body)


def test_off_is_refused_and_the_fields_are_none_like_today():
    c, _ = _client()
    r = _patch(c, pair_list=["ETH/USDT"])
    assert r.status_code == 409
    me = c.get("/api/account").json()
    assert me["live_pair_list"] is None and me["live_open_risk_cap_pct"] is None and me["live_pair_count"] == 1


def test_the_happy_path_stores_the_list_the_bot_already_reads():
    _on()
    c, email = _client()
    assert _patch(c, live_slots=3).status_code == 200
    r = _patch(c, pair_list=["ETH/USDT", "bnb/usdt"])
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["live_pair_list"] == ["ETH/USDT", "BNB/USDT"] and me["live_pair_count"] == 3 and me["live_open_risk_cap_pct"] == 3.0
    with session_scope() as s:
        acct = s.query(Customer).filter(Customer.email == email).one().account
        assert live_multi.live_pairs(acct) == ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
    assert _patch(c, pair_list=[]).json()["live_pair_list"] == []
    assert c.get("/api/account").json()["live_pair_count"] == 1


@pytest.mark.parametrize("slots,body,code", [
    (1, {"pair_list": ["ETH/USDT"]}, 422),                        # one slot: no extra pairs
    (2, {"pair_list": ["ETH/USDT", "BNB/USDT"]}, 422),            # at most slots - 1
    (3, {"pair_list": ["ETH/USDT", "ETH/USDT"]}, 422),            # duplicates
    (3, {"pair_list": ["BTC/USDT"]}, 422),                        # the main pair again
    (3, {"pair_list": ["DOGE/USDT"]}, 422),                       # not a pair the service knows
    (3, {"pair_list": ["ETH/USDT", "BNB/USDT", "XRP/USDT"]}, 422),  # more than two
    (3, {"pair_list": ["ETH/USDT"], "pair_selection_mode": "auto"}, 422),
])
def test_the_rules_refuse_and_change_nothing(slots, body, code):
    _on()
    c, _ = _client()
    if slots > 1:
        assert _patch(c, live_slots=slots).status_code == 200
    before = c.get("/api/account").json()
    r = _patch(c, **body)
    assert r.status_code == code, r.text
    if slots == 1:
        assert "Set the number of open trades to 2 or 3" in r.json()["detail"]
    after = c.get("/api/account").json()
    assert after["live_pair_list"] == before["live_pair_list"] and after["pair_selection_mode"] == before["pair_selection_mode"]


def test_auto_select_is_refused_while_a_list_is_chosen():
    _on()
    c, _ = _client()
    _patch(c, live_slots=2)
    assert _patch(c, pair_list=["ETH/USDT"]).status_code == 200
    assert _patch(c, pair_selection_mode="auto").status_code == 422  # slots above 1 already refuses Auto Select


def test_a_pair_with_an_open_trade_cannot_be_dropped():
    _on()
    c, email = _client()
    _patch(c, live_slots=2)
    assert _patch(c, pair_list=["ETH/USDT"]).status_code == 200
    with session_scope() as s:
        acct = s.query(Customer).filter(Customer.email == email).one().account
        s.add(Trade(account_id=acct.id, pair="ETH/USDT", side="long", is_paper=False, entry_price=100.0, quantity=0.01, stop_loss_price=95.0, status="open"))
    r = _patch(c, pair_list=["BNB/USDT"])
    assert r.status_code == 409 and "ETH/USDT" in r.json()["detail"]
    assert c.get("/api/account").json()["live_pair_list"] == ["ETH/USDT"]
    assert _patch(c, pair_list=["ETH/USDT"]).status_code == 200  # keeping it is fine


def test_the_cap_is_read_only():
    _on()
    c, _ = _client()
    r = _patch(c, live_open_risk_cap_pct=50, live_total_risk_cap_pct=50)
    assert r.status_code == 200 and r.json()["live_open_risk_cap_pct"] == 3.0  # unknown fields are ignored; the cap comes from the service setting only
