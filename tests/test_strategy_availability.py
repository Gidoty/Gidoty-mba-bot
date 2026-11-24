"""Strategy availability (docs/safety/strategy_availability.md): which strategies customers may choose, what a new account starts with, and the setup advisor's candle-size preview.

PROVES: everything is off or EMA-only by default and an unchanged service behaves as before; the customer routes (list, choose) obey the switch, the availability, the open-trade rule and validation, and only ever write
`extra_strategies`; a new account gets the configured default only for strategies that are available; availability never changes an account that is already set up and never decides whether a strategy trades;
the operator-settings table drives the same values; the advisor's preview says whether the protection check would refuse the stop at the customer's leverage, whether the order is under the exchange minimum and how many
signals a year to expect, uses the bot's own functions, saves nothing and is silent unless asked. DOES NOT PROVE the live exchange minimums or brackets (UNVERIFIED), or the screens."""
from __future__ import annotations

import dataclasses
import os
import random
import tempfile
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_strategy_availability.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot import multi_trade, setup_advice as sa, strategy_availability as sv  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.deps import get_settings  # noqa: E402
from mba_bot.config import load_settings  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.indicators import ema  # noqa: E402
from mba_bot.models import Customer, OperatorSetting, TradingAccount  # noqa: E402

DON, RSI = "donchian_breakout_v1", "rsi_mr_v1"
_counter = iter(range(1, 99_999))


def _override(**kw):
    app.dependency_overrides[get_settings] = lambda: dataclasses.replace(load_settings(), **kw)


@pytest.fixture(autouse=True)
def _clean():
    yield
    app.dependency_overrides.pop(get_settings, None)


def _signup(client, *, market="spot", capital=1000.0, **extra):
    n = next(_counter)
    email = f"sv{n}@example.com"
    assert client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True}).status_code == 201
    r = client.post("/api/account", json={"pair": "BTC/USDT", "market_type": market, "notification_channel": "telegram", "api_key": "k", "api_secret": "s"})
    assert r.status_code == 201, r.text
    if capital is not None:
        assert client.patch("/api/account", json={"allocated_capital": capital, **extra}).status_code == 200
    return email


def _account(email) -> TradingAccount:
    with session_scope() as s:
        return s.query(TradingAccount).join(Customer).filter(Customer.email == email).one()


# ---------------------------------------------------------------- defaults
def test_everything_is_off_or_ema_only_by_default():
    s = load_settings()
    assert (s.customer_strategy_choice, s.strategy_donchian_available, s.strategy_rsi_available, s.new_account_strategies) == (False, False, False, "ema_only")
    assert sv.available(s) == ("ema_cross_atr_v1",) and sv.default_for_new(s) is None


def test_a_typo_in_the_default_for_new_accounts_is_ema_only_and_a_valid_value_is_read(monkeypatch):
    for raw in ("rsi", "", "everything", "ema_rsi,donchian"):
        monkeypatch.setenv("NEW_ACCOUNT_STRATEGIES", raw)
        assert load_settings().new_account_strategies == "ema_only", raw
    monkeypatch.setenv("NEW_ACCOUNT_STRATEGIES", " EMA_RSI ")
    assert load_settings().new_account_strategies == "ema_rsi"


def test_a_new_account_gets_the_default_only_for_available_strategies():
    base = load_settings()
    assert sv.default_for_new(dataclasses.replace(base, new_account_strategies="ema_donchian_rsi")) is None  # nothing is available
    assert sv.default_for_new(dataclasses.replace(base, new_account_strategies="ema_donchian_rsi", strategy_rsi_available=True)) == RSI
    both = dataclasses.replace(base, new_account_strategies="ema_donchian_rsi", strategy_rsi_available=True, strategy_donchian_available=True)
    assert sv.default_for_new(both) == f"{DON},{RSI}"
    assert sv.default_for_new(dataclasses.replace(base, new_account_strategies="ema_only", strategy_rsi_available=True)) is None


def test_account_creation_uses_the_default_and_an_unchanged_service_creates_ema_only_accounts():
    with TestClient(app, base_url="https://testserver") as c:
        e1 = _signup(c)
        assert _account(e1).extra_strategies is None
    _override(new_account_strategies="ema_donchian", strategy_donchian_available=True)
    with TestClient(app, base_url="https://testserver") as c:
        e2 = _signup(c)
        assert _account(e2).extra_strategies == DON
    _override(new_account_strategies="ema_donchian")  # chosen as the default but not available
    with TestClient(app, base_url="https://testserver") as c:
        assert _account(_signup(c)).extra_strategies is None


# ---------------------------------------------------------------- the customer routes
def test_the_list_says_what_is_available_selected_and_whether_it_trades_live(monkeypatch):
    _override(strategy_rsi_available=True, live_rsi_mr=True)
    with TestClient(app, base_url="https://testserver") as c:
        _signup(c)
        body = c.get("/api/account/strategies").json()
        assert body["can_choose"] is False and body["selected"] == ["ema_cross_atr_v1"]
        by = {s["id"]: s for s in body["strategies"]}
        assert by["ema_cross_atr_v1"]["available"] and by[RSI]["available"] and not by[DON]["available"]
        assert by[RSI]["trades_live_now"] is True and by[DON]["trades_live_now"] is False
        assert all("buy" not in s["description"].lower() for s in body["strategies"])


def test_choosing_needs_the_switch_the_availability_and_no_open_trade():
    from admin_support import mk_trade

    with TestClient(app, base_url="https://testserver") as c:
        email = _signup(c)
        assert c.put("/api/account/strategies", json={"strategies": []}).status_code == 403  # switch off
        _override(customer_strategy_choice=True, strategy_donchian_available=True)
        assert c.put("/api/account/strategies", json={"strategies": [DON]}).json()["selected"] == ["ema_cross_atr_v1", DON]
        assert _account(email).extra_strategies == DON
        assert c.put("/api/account/strategies", json={"strategies": [RSI]}).status_code == 422  # not available
        assert c.put("/api/account/strategies", json={"strategies": ["nope"]}).status_code == 422
        assert c.put("/api/account/strategies", json={"strategies": [DON, DON]}).status_code == 422
        assert c.put("/api/account/strategies", json={"strategies": "x"}).status_code == 422
        assert c.put("/api/account/strategies", json={"strategies": [DON, RSI, "ema_cross_atr_v1", "x"]}).status_code == 422
        assert _account(email).extra_strategies == DON  # every refusal left it alone
        with session_scope() as s:
            mk_trade(s, s.get(TradingAccount, _account(email).id), status="open", paper=False, pnl=None)
        r = c.put("/api/account/strategies", json={"strategies": []})
        assert r.status_code == 409 and _account(email).extra_strategies == DON
    with TestClient(app, base_url="https://testserver") as anon:
        assert anon.put("/api/account/strategies", json={"strategies": []}).status_code == 401


def test_choosing_ema_only_clears_the_extras_and_the_choice_writes_nothing_else():
    _override(customer_strategy_choice=True, strategy_donchian_available=True, strategy_rsi_available=True)
    with TestClient(app, base_url="https://testserver") as c:
        email = _signup(c)
        before = {k: getattr(_account(email), k) for k in ("pair", "market_type", "risk_pct", "is_active", "max_leverage", "allocated_capital", "bot_started_at", "live_trading_enabled_at")}
        c.put("/api/account/strategies", json={"strategies": [RSI, DON]})
        assert set(multi_trade.account_strategies(_account(email))) == {RSI, DON}
        r = c.put("/api/account/strategies", json={"strategies": ["ema_cross_atr_v1"]})
        assert r.json()["selected"] == ["ema_cross_atr_v1"] and _account(email).extra_strategies is None
        assert {k: getattr(_account(email), k) for k in before} == before


def test_availability_never_changes_an_account_already_set_up_or_decides_what_trades():
    with TestClient(app, base_url="https://testserver") as c:
        email = _signup(c)
    with session_scope() as s:
        s.get(TradingAccount, _account(email).id).extra_strategies = RSI
    _override(strategy_rsi_available=False, strategy_donchian_available=False, new_account_strategies="ema_only")
    assert set(multi_trade.account_strategies(_account(email))) == {RSI}  # still opted in; whether it trades is the live switch's job
    with TestClient(app, base_url="https://testserver") as c:
        c.cookies.clear()
    import inspect
    from mba_bot import trader, scheduler

    for mod in (trader, scheduler):
        assert "strategy_availability" not in inspect.getsource(mod)


def test_the_operator_table_drives_the_same_values(tmp_path):
    from mba_bot import operator_settings as O

    base = load_settings()
    with session_scope() as s:
        s.merge(OperatorSetting(key="strategy_donchian_available", value="true"))
        s.merge(OperatorSetting(key="new_account_strategies", value="ema_donchian"))
        s.merge(OperatorSetting(key="customer_strategy_choice", value="true"))
    O.reset_cache()
    eff = O.effective_settings(base)
    assert sv.available(eff) == ("ema_cross_atr_v1", DON) and sv.default_for_new(eff) == DON and eff.customer_strategy_choice is True
    assert {"customer_strategy_choice", "strategy_donchian_available", "strategy_rsi_available"} <= set(O.customer_view(base))
    assert "new_account_strategies" not in O.customer_view(base)
    with session_scope() as s:
        for k in ("strategy_donchian_available", "new_account_strategies", "customer_strategy_choice"):
            s.delete(s.get(OperatorSetting, k))
    O.reset_cache()


# ---------------------------------------------------------------- the advisor's candle-size preview
def _facts(**kw):
    base = dict(switch_multi=False, risk_pct_max=2.0, max_notional_multiplier=3.0, futures_max_leverage=None, live_total_risk_cap_pct=3.0, donchian_risk_pct=0.5, rsi_risk_pct=0.5, rsi_live=False,
                donchian_live=False, daily_limit_now=None, reference_value=None, pair_min_notional={"BTC/USDT": 100.0})
    base.update(kw)
    return sa.Facts(**base)


def _acct(**kw):
    base = dict(pair="BTC/USDT", market_type="futures", risk_pct=1.0, allocated_capital=1000.0, live_slots=1, pair_selection_mode="manual", max_leverage=3, extra_strategies=None, pair_list=None, daily_loss_limit_usd=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _notes(account, tf, **facts):
    return {n["id"]: n for n in sa.advise(account, _facts(**facts), sa.Proposed(ema_timeframe=tf))}


def test_the_preview_is_silent_unless_asked():
    assert not [n for n in sa.advise(_acct(), _facts()) if n["id"].startswith("tf_")]
    assert not [n for n in sa.advise(_acct(), _facts(), sa.Proposed(risk_pct=1.0)) if n["id"].startswith("tf_")]


def test_an_unknown_candle_size_gets_one_plain_note():
    n = _notes(_acct(), "7m")
    assert n["tf_unknown"]["severity"] == "warning" and "tf_stop" not in n and "1h, 4h or 1d" in n["tf_unknown"]["text"]


def test_the_protection_check_verdict_uses_the_bots_own_function():
    from mba_bot import risk

    for tf in ("1h", "4h", "1d"):
        for lev in (3, 10, 20, 50):
            n = _notes(_acct(), tf, binance_leverage=float(lev), binance_pair="BTC/USDT")
            stop = sa.stop_pct_for(tf) / 100.0
            m, t = 1.0 / lev, 0.005
            should_pass = (m - t) > 0 and stop <= 0.75 * (m - t)
            assert (n["tf_stop"]["severity"] == "tip") is should_pass, (tf, lev)
            assert ("would refuse these entries" in n["tf_stop"]["text"]) is (not should_pass)
    assert risk.assess_stop_viability  # the same function the entry path calls


def test_a_wide_stop_at_high_leverage_is_refused_with_the_allowed_distance_and_a_lever_to_pull():
    n = _notes(_acct(), "1d", binance_leverage=20.0, binance_pair="BTC/USDT")["tf_stop"]
    assert n["severity"] == "warning" and "4.9%" in n["text"] and "3.4%" in n["text"] and "20x, the leverage set on Binance" in n["text"] and n["action"] and "lower leverage" in n["action"].lower()
    ok = _notes(_acct(max_leverage=3), "1d")["tf_stop"]  # Binance's number is not known: the customer's limit (3x) is used and the text says so
    assert ok["severity"] == "tip" and ok["action"] is None and "your leverage limit of 3x" in ok["text"] and "not known here" in ok["text"]


def test_spot_has_no_liquidation_check():
    n = _notes(_acct(market_type="spot"), "1d")
    assert "does not apply" in n["tf_stop"]["text"] and "tf_order" not in n


def test_the_minimum_order_note_follows_capital_risk_and_the_exchange_minimum():
    small = _notes(_acct(allocated_capital=50.0), "1d")["tf_order"]
    assert small["severity"] == "warning" and "minimum order" in small["text"] and "10.21" in small["text"] and small["action"]
    big = _notes(_acct(allocated_capital=1000.0), "1d")["tf_order"]
    assert big["severity"] == "tip" and "above the exchange's minimum" in big["text"]
    unknown = _notes(_acct(), "1d", pair_min_notional={})["tf_order"]
    assert unknown["severity"] == "tip" and "could not be read" in unknown["text"]
    assert _notes(_acct(allocated_capital=50.0), "4h")["tf_order"]["severity"] == "warning"  # 2% stop: 25 USDT order against a minimum of 100
    none = _notes(_acct(allocated_capital=0.0), "1d")
    assert {"tf_signals", "no_capital"} <= none.keys() and "tf_order" not in none


def test_the_signal_count_is_a_property_of_the_indicator_and_shrinks_with_the_candle_length():
    per_year = {tf: float(_notes(_acct(), tf)["tf_signals"]["text"].split("about ")[2].split(" times")[0]) for tf in ("1h", "4h", "1d")}
    assert per_year == {"1h": 180.0, "4h": 44.0, "1d": 7.3}
    text = _notes(_acct(), "1d")["tf_signals"]["text"]
    assert "not a forecast of results" in text and "skips flat markets" in text and "one open trade at a time" in text


def test_the_crossing_constant_matches_a_fresh_random_series():
    rng = random.Random(777)
    price, closes = 100.0, []
    for _ in range(150_000):
        price *= 1 + rng.gauss(0, 0.01)
        closes.append(price)
    fast, slow = ema(closes, 20), ema(closes, 50)
    n = sum(1 for i in range(1, len(closes)) if None not in (fast[i], fast[i - 1], slow[i], slow[i - 1]) and ((fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]) or (fast[i - 1] >= slow[i - 1] and fast[i] < slow[i])))
    assert abs(n / len(closes) - sa.CROSSINGS_PER_CANDLE) / sa.CROSSINGS_PER_CANDLE < 0.15


def test_the_preview_saves_nothing_and_the_endpoint_returns_it():
    _override(setup_advisor=True)
    with TestClient(app, base_url="https://testserver") as c:
        email = _signup(c, market="futures")
        before = c.get("/api/account").json()
        r = c.post("/api/account/advice", json={"ema_timeframe": "1d"})
        assert r.status_code == 200 and {"tf_signals", "tf_stop"} <= {n["id"] for n in r.json()["notes"]}
        assert c.get("/api/account").json() == before
        assert not [n for n in c.post("/api/account/advice", json={}).json()["notes"] if n["id"].startswith("tf_")]
        assert any(n["id"] == "tf_unknown" for n in c.post("/api/account/advice", json={"ema_timeframe": "9x"}).json()["notes"])
    assert _account(email).extra_strategies is None
