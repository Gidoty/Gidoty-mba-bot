"""B4: the demo runner (DEMO_RUNNER, default off).

PROVES (fake exchange, scratch SQLite): the mode resolves to DEMO only with the switch on, demo keys on file, the customer's explicit demo action, a paid account and NO live authorisation; a DEMO cycle
builds a sandbox client with the customer's DEMO keys (never the live credential), runs the full live entry (protective stop, verified), records the trade as is_demo (never paper, never counted as
live), labels every message DEMO and none LIVE, and does not touch the live account's equity samples or loss limits; with the switch off the same account behaves as paper; the API refuses demo
keys that equal the live keys or that Binance does not accept on the demo endpoint, stores them encrypted, refuses live trading while demo is on and demo while live is on, and the trade lists keep paper,
demo and live apart; the migration is additive and reversible.
DOES NOT PROVE: that Binance Futures Demo Trading and Spot Testnet accept every call the live path makes (UNVERIFIED: the fake exchange stands in), nor the screens in a browser."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_demo_runner.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402
from helpers_live import BULLISH_OHLCV, FakeNotifier, all_trades, futures_client, make_settings, new_account, run_cycle, spot_client, the_trade  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402
from test_api_live_trading_safety import _make_eligible, _mark_paid, _retag_mainnet, _signup_with_account  # noqa: E402

from mba_bot import demo as demo_module, exchange, lifecycle, venue  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.routers import account as acct_router  # noqa: E402
from mba_bot.crypto_utils import decrypt, encrypt  # noqa: E402
from mba_bot.db import init_db, session_scope  # noqa: E402
from mba_bot.models import AccountDayEquity, Customer, DemoCredential, Trade, TradingAccount  # noqa: E402
from mba_bot.onboarding import TradingMode, resolve_trading_mode  # noqa: E402
from mba_bot.strategy_guards import closed_results  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEMO_KEY, DEMO_SECRET = "demo-key-AAAA", "demo-secret-BBBB"


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


_n = __import__("itertools").count()


def demo_account(settings, *, market="futures", enabled=True, live=False, keys=True, **kw):
    kw.setdefault("email", f"demo{next(_n)}@example.com")
    aid = new_account(settings, market_type=market, **kw)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        if not live:
            a.live_trading_enabled_at = None
            a.bot_started_at = None
        if enabled:
            a.demo_enabled_at = datetime.now(timezone.utc)
        if keys:
            s.add(DemoCredential(account_id=aid, encrypted_api_key=encrypt(DEMO_KEY, key=settings.encryption_key), encrypted_api_secret=encrypt(DEMO_SECRET, key=settings.encryption_key)))
    return aid


def install_recording(monkeypatch, client):
    built = []

    def build(**kw):
        built.append(kw)
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", build)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(venue, "_sleep", _no_sleep)
    monkeypatch.setattr(lifecycle, "_sleep", _no_sleep)
    return built


def demo_settings(**kw):
    return make_settings(max_notional_multiplier=100.0, demo_runner=True, protection_precheck_mode="off", **kw)


# ------------------------------------------------------------------ the mode
def _mode(**over):
    base = dict(trial_ends_at=datetime.now(timezone.utc) - timedelta(days=30), live_bypass_accepted_at=None, has_paid=True, live_trading_enabled_at=None, has_mainnet_credential=False, bot_started_at=None)
    base.update(over)
    return resolve_trading_mode(**base).mode


def test_demo_resolves_only_with_the_explicit_flag_and_never_next_to_live():
    assert _mode() == TradingMode.PAPER and _mode(demo_enabled=False) == TradingMode.PAPER
    assert _mode(demo_enabled=True) == TradingMode.DEMO
    assert _mode(demo_enabled=True, has_paid=False) == TradingMode.PAPER  # unpaid: nothing runs beyond paper
    now = datetime.now(timezone.utc)
    assert _mode(demo_enabled=True, live_trading_enabled_at=now) != TradingMode.DEMO  # live-authorised: demo never wins
    assert _mode(demo_enabled=True, bot_started_at=now) != TradingMode.DEMO
    assert _mode(live_trading_enabled_at=now, has_mainnet_credential=True, bot_started_at=now) == TradingMode.LIVE  # live is exactly as before


def test_demo_is_refused_when_live_trading_is_enabled_even_with_demo_on_keys_on_file_and_the_bot_not_started(db):
    """The two live conditions are checked on their own: live_trading_enabled_at alone (bot_started_at empty) already refuses DEMO, and so does bot_started_at alone."""
    st = demo_settings()
    base = demo_account(st)  # demo switched on, demo keys on file, no live flags
    with session_scope() as s:
        a = s.get(TradingAccount, base)
        assert demo_module.available(a, st) is True and a.bot_started_at is None  # the control: everything else in place
        a.live_trading_enabled_at = datetime.now(timezone.utc)  # live authorised, Start Bot NOT pressed
        assert a.demo_enabled_at is not None and a.demo_credential is not None and a.bot_started_at is None
        assert demo_module.available(a, st) is False
        a.live_trading_enabled_at = None
        a.bot_started_at = datetime.now(timezone.utc)  # the other flag alone
        assert demo_module.available(a, st) is False


def test_available_needs_the_switch_the_action_the_keys_and_no_live(db):
    st = demo_settings()
    ok = demo_account(st)
    with session_scope() as s:
        a = s.get(TradingAccount, ok)
        assert demo_module.available(a, st) is True
        import dataclasses

        assert demo_module.available(a, dataclasses.replace(st, demo_runner=False)) is False  # switch off
        a.demo_enabled_at = None
        assert demo_module.available(a, st) is False  # not switched on by the customer
    nokeys = demo_account(st, keys=False)
    with session_scope() as s:
        assert demo_module.available(s.get(TradingAccount, nokeys), st) is False
    livenow = demo_account(st, live=True)
    with session_scope() as s:
        assert demo_module.available(s.get(TradingAccount, livenow), st) is False


# ------------------------------------------------------------------ the cycle
@pytest.mark.parametrize("market", ["futures", "spot"])
def test_a_demo_cycle_uses_the_demo_keys_on_a_sandbox_client_and_opens_a_protected_demo_trade(db, monkeypatch, market):
    st = demo_settings()
    client = (futures_client if market == "futures" else spot_client)(ohlcv=BULLISH_OHLCV)
    built = install_recording(monkeypatch, client)
    aid = demo_account(st, market=market)
    n = run_cycle(aid, st)
    assert built and all(b["sandbox"] is True and b["api_key"] == DEMO_KEY and b["api_secret"] == DEMO_SECRET for b in built)  # never the live credential, always the sandbox
    t = the_trade(aid)
    assert t.is_demo is True and t.is_paper is False and t.status == "open" and t.protection_status == "protected"  # the full path, with the protective stop
    assert any("[DEMO] Entered" in x for x in n.texts) and not any("[LIVE]" in x for x in n.texts)


def test_the_live_credential_is_never_read_in_demo_mode(db, monkeypatch):
    st = demo_settings()
    aid = demo_account(st)
    reads = []
    real = decrypt

    def spying(token, key):
        reads.append(token)
        return real(token, key=key)

    monkeypatch.setattr(demo_module, "decrypt", spying)
    from mba_bot import trader

    monkeypatch.setattr(trader, "decrypt", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the live credential must not be decrypted in demo mode")))
    client = futures_client(ohlcv=BULLISH_OHLCV)
    install_recording(monkeypatch, client)
    run_cycle(aid, st)
    assert len(reads) == 2  # the demo key and secret only
    assert the_trade(aid).is_demo is True


def test_the_same_setup_as_a_live_account_does_sample_its_equity(db, monkeypatch):
    """The control for the test below: with the account live, the equity sample IS written, so the demo test proves a real exclusion."""
    st = demo_settings(account_daily_loss_limit=True, account_total_loss_stop=True)
    client = futures_client(ohlcv=BULLISH_OHLCV)
    client.total_margin_balance = 1000.0
    install_recording(monkeypatch, client)
    aid = new_account(st, market_type="futures", email="control-live@example.com")
    run_cycle(aid, st)
    with session_scope() as s:
        assert s.query(AccountDayEquity).count() == 1


def test_a_demo_cycle_does_not_touch_the_live_loss_limits(db, monkeypatch):
    st = demo_settings(account_daily_loss_limit=True, account_total_loss_stop=True)
    client = futures_client(ohlcv=BULLISH_OHLCV)
    client.total_margin_balance = 1000.0  # a readable balance: a live cycle WOULD sample it
    install_recording(monkeypatch, client)
    aid = demo_account(st)
    run_cycle(aid, st)
    with session_scope() as s:
        assert s.query(AccountDayEquity).count() == 0
        assert s.get(TradingAccount, aid).loss_reference_equity is None  # the live account's equity references are never set from demo money


def test_the_demo_trade_exits_labelled_demo(db, monkeypatch):
    from helpers_live import BEAR_PRICE, BEARISH_OHLCV

    st = demo_settings()
    client = futures_client(ohlcv=BULLISH_OHLCV)
    install_recording(monkeypatch, client)
    aid = demo_account(st)
    run_cycle(aid, st)
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    n = run_cycle(aid, st)
    t = the_trade(aid)
    assert t.status == "closed" and t.is_demo is True
    assert any("[DEMO] Exited" in x for x in n.texts) and not any("[LIVE]" in x for x in n.texts)


def test_with_the_switch_off_the_account_is_an_ordinary_paper_account(db, monkeypatch):
    import dataclasses

    st = dataclasses.replace(demo_settings(), demo_runner=False, demo_futures_api_key="paper-k", demo_futures_api_secret="paper-s")
    client = futures_client(ohlcv=BULLISH_OHLCV)
    built = install_recording(monkeypatch, client)
    aid = demo_account(st)
    run_cycle(aid, st)
    assert built and all(b["api_key"] == "paper-k" for b in built)  # the platform's paper keys, as today; the customer's demo keys are not used
    assert not any(t.is_demo for t in all_trades(aid))


def test_a_live_account_that_also_has_demo_keys_trades_live_with_its_live_credential(db, monkeypatch):
    st = demo_settings()
    client = futures_client(ohlcv=BULLISH_OHLCV)
    built = install_recording(monkeypatch, client)
    aid = demo_account(st, live=True, enabled=True)  # an ambiguous row: live authorised AND demo flagged
    run_cycle(aid, st)
    assert built and all(b["sandbox"] is False and b["api_key"] != DEMO_KEY for b in built)
    assert the_trade(aid).is_demo is False


def test_strategy_guard_history_never_counts_demo_trades(db):
    aid = new_account(make_settings())
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        for demo in (False, True):
            s.add(Trade(account_id=aid, side="long", is_paper=False, is_demo=demo, entry_price=100.0, quantity=1.0, stop_loss_price=95.0, status="closed", pair="BTC/USDT", exit_price=90.0, pnl=-10.0,
                        opened_at=now - timedelta(hours=2), closed_at=now - timedelta(hours=1), close_reason="stop_loss"))
    with session_scope() as s:
        assert len(closed_results(s, aid, is_paper=False, fee_bps=0.0)) == 1


# ------------------------------------------------------------------ the API
def _signed_up(email, *, capital=1000.0):
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    _signup_with_account(client, email)
    if capital is not None:
        client.patch("/api/account", json={"allocated_capital": capital})
    _mark_paid(email)
    return client


@pytest.fixture
def demo_on():
    import dataclasses

    from mba_bot.api.deps import get_settings
    from mba_bot.config import load_settings

    app.dependency_overrides[get_settings] = lambda: dataclasses.replace(load_settings(), demo_runner=True)
    yield
    app.dependency_overrides.pop(get_settings, None)


def test_the_demo_routes_are_refused_while_the_switch_is_off():
    client = _signed_up("demo-off@example.com")
    try:
        assert client.put("/api/account/demo-credentials", json={"api_key": "a", "api_secret": "b"}).status_code == 409
        assert client.post("/api/account/demo-mode", json={"enabled": True, "confirm": True}).status_code == 409
        me = client.get("/api/account").json()
        assert me["demo_available"] is False and me["demo_has_keys"] is False and me["mode"] == "paper"
    finally:
        client.__exit__(None, None, None)


def test_demo_keys_are_checked_on_the_demo_endpoint_stored_encrypted_and_never_returned(demo_on, monkeypatch):
    email = "demo-keys@example.com"
    client = _signed_up(email)
    try:
        calls = []

        async def accepted(account, key, secret):
            calls.append((key, secret, account.market_type))
            return key == DEMO_KEY

        monkeypatch.setattr(acct_router, "_demo_keys_accepted", accepted)
        refused = client.put("/api/account/demo-credentials", json={"api_key": "REAL-KEY", "api_secret": "REAL-SECRET"})
        assert refused.status_code == 422 and refused.json()["detail"] == demo_module.MSG_KEYS_REFUSED and "REAL-KEY" not in refused.text  # a key Binance rejects on demo is refused
        assert client.get("/api/account").json()["demo_has_keys"] is False
        ok = client.put("/api/account/demo-credentials", json={"api_key": DEMO_KEY, "api_secret": DEMO_SECRET})
        assert ok.status_code == 200 and ok.json()["demo_has_keys"] is True and DEMO_KEY not in ok.text and DEMO_SECRET not in ok.text
        with session_scope() as s:
            cid = s.query(Customer).filter(Customer.email == email).one().id
            row = s.query(DemoCredential).join(TradingAccount, TradingAccount.id == DemoCredential.account_id).filter(TradingAccount.customer_id == cid).one()
            assert DEMO_KEY not in row.encrypted_api_key and DEMO_SECRET not in row.encrypted_api_secret  # encrypted at rest
    finally:
        client.__exit__(None, None, None)


def test_a_demo_key_equal_to_the_live_key_is_refused_without_any_call(demo_on, monkeypatch):
    email = "same-key@example.com"
    client = _signed_up(email)
    try:
        client.put("/api/account/credentials", json={"api_key": "the-live-key", "api_secret": "the-live-secret", "environment": "mainnet"})

        async def boom(*a):
            raise AssertionError("no exchange call for a key that is the live key")

        monkeypatch.setattr(acct_router, "_demo_keys_accepted", boom)
        r = client.put("/api/account/demo-credentials", json={"api_key": "the-live-key", "api_secret": "x"})
        assert r.status_code == 422 and r.json()["detail"] == demo_module.MSG_SAME_AS_LIVE
    finally:
        client.__exit__(None, None, None)


def test_demo_mode_needs_keys_capital_and_a_non_live_account_and_blocks_live_trading(demo_on, monkeypatch):
    email = "demo-flow@example.com"
    client = _signed_up(email, capital=None)
    try:
        async def accepted(account, key, secret):
            return True

        monkeypatch.setattr(acct_router, "_demo_keys_accepted", accepted)
        assert client.post("/api/account/demo-mode", json={"enabled": True, "confirm": True}).json()["detail"] == demo_module.MSG_NEED_KEYS
        assert client.put("/api/account/demo-credentials", json={"api_key": DEMO_KEY, "api_secret": DEMO_SECRET}).status_code == 200
        no_cap = client.post("/api/account/demo-mode", json={"enabled": True, "confirm": True})
        assert no_cap.status_code == 409 and "trading capital" in no_cap.json()["detail"]
        client.patch("/api/account", json={"allocated_capital": 500})
        assert client.post("/api/account/demo-mode", json={"enabled": True, "confirm": False}).status_code == 422  # needs an explicit confirmation
        on = client.post("/api/account/demo-mode", json={"enabled": True, "confirm": True})
        assert on.status_code == 200 and on.json()["mode"] == "demo" and on.json()["demo_enabled_at"] is not None
        # live trading cannot be enabled while demo is on
        _retag_mainnet(client)
        _make_eligible(email)
        client.patch("/api/account", json={"daily_loss_limit_usd": 5})
        blocked = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert blocked.status_code == 409 and blocked.json()["detail"] == demo_module.MSG_LIVE_BLOCKED
        off = client.post("/api/account/demo-mode", json={"enabled": False, "confirm": True})
        assert off.status_code == 200 and off.json()["mode"] == "paper" and off.json()["demo_enabled_at"] is None
        assert client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True}).status_code == 200
        # and demo cannot be switched on once live is enabled
        again = client.post("/api/account/demo-mode", json={"enabled": True, "confirm": True})
        assert again.status_code == 409 and again.json()["detail"] == demo_module.MSG_LIVE_ACTIVE
    finally:
        client.__exit__(None, None, None)


def test_the_trade_lists_keep_paper_demo_and_live_apart(db):
    from mba_bot.api.routers.trades import _filter_is_paper

    mk = lambda paper, demo: Trade(account_id="a", side="long", is_paper=paper, is_demo=demo, entry_price=1.0, quantity=1.0, stop_loss_price=0.5, status="closed", opened_at=datetime.now(timezone.utc))
    paper, demo, live = mk(True, False), mk(False, True), mk(False, False)
    allt = [paper, demo, live]
    assert _filter_is_paper(allt, True) == [paper] and _filter_is_paper(allt, False) == [live] and _filter_is_paper(allt, None, True) == [demo]
    assert _filter_is_paper(allt, None) == allt  # the unscoped call is unchanged


# ------------------------------------------------------------------ the migration and the code
def test_the_migration_is_additive_idempotent_and_reversible(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "f4c7d9b2a6e1")
    insp = lambda: inspect(create_engine(f"sqlite:///{path}"))
    cols = lambda t: {c["name"] for c in insp().get_columns(t)}
    a0, t0, tables0 = cols("trading_accounts"), cols("trades"), set(insp().get_table_names())
    command.upgrade(cfg, "a6e2c4f8b1d3")
    assert cols("trading_accounts") == a0 | {"demo_enabled_at"} and cols("trades") == t0 | {"is_demo"} and set(insp().get_table_names()) == tables0 | {"demo_credentials"}
    command.upgrade(cfg, "a6e2c4f8b1d3")
    command.downgrade(cfg, "f4c7d9b2a6e1")
    assert cols("trading_accounts") == a0 and cols("trades") == t0 and set(insp().get_table_names()) == tables0


def test_the_live_path_labels_come_from_one_function_and_a_demo_client_is_always_a_sandbox_client():
    src = (ROOT / "src/mba_bot/lifecycle.py").read_text()
    assert '"[LIVE] ' not in src and 'f"[LIVE] ' not in src  # no hard-coded LIVE label left in the lifecycle messages
    trader_src = (ROOT / "src/mba_bot/trader.py").read_text()
    assert "sandbox=effective_is_paper or effective_is_demo" in trader_src
    assert "_resolve_credentials(account, settings, is_paper=effective_is_paper, is_demo=effective_is_demo)" in trader_src


def test_the_screens_label_demo_and_never_show_it_as_live():
    front = ROOT / "frontend" / "src"
    app, router, dash = (front / "App.jsx").read_text(), (front / "pages" / "DashboardRouter.jsx").read_text(), (front / "pages" / "dashboard" / "LiveDashboard.jsx").read_text()
    assert "<DemoBanner account={account} />" in app and 'account?.mode !== "demo"' in (front / "components" / "DemoBanner.jsx").read_text()
    assert router.index('account.mode === "demo"') < router.index('account.mode === "live"')  # demo is decided before live, with its own dashboard
    assert 'demo ? "DEMO" : "LIVE"' in dash and "is_demo=true" in dash
    for page in ("TradeHistory.jsx", "Performance.jsx", "Positions.jsx"):
        text = (front / "pages" / page).read_text()
        assert 'account.mode === "demo"' in text and "useTrades(isPaper, account.id, isDemo)" in text and "DEMO:" in text, page
    assert "is_demo=true" in (front / "components" / "dashboard" / "useTrades.js").read_text()
    panel = (front / "components" / "DemoPanel.jsx").read_text()
    assert "account.demo_available" in panel and "Do not use your live Binance keys here" in panel and 'type="password"' in panel
