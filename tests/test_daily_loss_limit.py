"""The customer's daily loss limit in dollars (docs/safety/account_daily_loss.md) and the fixed 50 percent total stop.

PROVES (fake exchange, scratch SQLite, frozen clock): the dollar rule (hit at the amount, not under it, unreadable equity and a missing amount fail closed, a new day starts clean); a raise starts only
at the next midnight Lagos time while a lowering applies at once; the amount is required before live trading is enabled and before Start Bot (the server refuses, not only the screen); the amount must
be above 0 and below the dollar level of the total stop; the one-time default for existing live accounts (10 percent of the last stored value, else of the capital) runs once and touches nothing else;
the warning flag for one trade larger than the limit; the pause message carries the amount lost today; the total stop is 50 percent for every risk setting; the migration is additive and reversible;
nothing reads the old percent variable.
DOES NOT PROVE: anything about Binance, the screens in a browser, or the owner's real account values."""
from __future__ import annotations

import dataclasses
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_daily_loss_limit.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402
from helpers_live import BULLISH_OHLCV, FakeNotifier, all_trades, make_settings, new_account, run_cycle  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402
from test_account_daily_loss import LIMIT_USD, NOON, live_orders, live_settings, rig, rows  # noqa: E402
from test_api_live_trading_safety import _make_eligible, _mark_paid, _retag_mainnet, _signup_with_account  # noqa: E402

from mba_bot import account_loss, daily_limit as dl, risk_limits as rl  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.db import init_db, session_scope  # noqa: E402
from mba_bot.models import AccountDayEquity, AccountLossPause, TradingAccount  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
U = timezone.utc


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def acct(**kw):
    base = dict(daily_loss_limit_usd=None, daily_loss_limit_pending_usd=None, daily_loss_limit_pending_from=None, daily_loss_limit_is_default=False)
    base.update(kw)
    return SimpleNamespace(**base)


# ------------------------------------------------------------------ pure rules
def test_the_amount_must_be_above_zero_and_below_the_total_stop_level():
    assert dl.validate_amount(100, reference_value=1000) is None and dl.validate_amount(499.99, reference_value=1000) is None
    for bad in (0, -1, None, float("nan"), float("inf"), True, "5"):
        assert dl.validate_amount(bad, reference_value=1000) is not None
    assert dl.validate_amount(500, reference_value=1000) is not None  # exactly the total stop level: the daily limit may not reach it
    assert "500.00" in dl.validate_amount(600, reference_value=1000)
    assert dl.validate_amount(10_000, reference_value=None) is None  # no reference yet: only the above-zero rule can apply


def test_suggestion_percent_and_the_one_trade_warning():
    assert dl.suggested_usd(1000) == 50.0 and dl.suggested_usd(None) is None and dl.suggested_usd(0) is None
    assert dl.pct_of(50, 1000) == 5.0 and dl.pct_of(50, 0) is None and dl.pct_of(None, 1000) is None
    assert dl.one_trade_can_exceed(5, 1000, 40) is True  # 5% of 1000 = 50 > 40
    assert dl.one_trade_can_exceed(5, 1000, 50) is False and dl.one_trade_can_exceed(1, 1000, 40) is False  # 10 <= 40
    assert dl.one_trade_can_exceed(None, 1000, 40) is False and dl.one_trade_can_exceed(5, None, 40) is False and dl.one_trade_can_exceed(5, 1000, None) is False


def test_a_lowering_applies_at_once_and_a_raise_waits_for_the_next_midnight():
    a = acct()
    assert dl.set_amount(a, 50, today_key="2027-03-10", next_day_key="2027-03-11") == "set"  # the first amount is in force at once
    assert a.daily_loss_limit_usd == 50 and a.daily_loss_limit_pending_usd is None
    assert dl.set_amount(a, 30, today_key="2027-03-10", next_day_key="2027-03-11") == "set" and a.daily_loss_limit_usd == 30  # lowering: at once
    assert dl.set_amount(a, 80, today_key="2027-03-10", next_day_key="2027-03-11") == "pending"
    assert (a.daily_loss_limit_usd, a.daily_loss_limit_pending_usd, a.daily_loss_limit_pending_from) == (30, 80, "2027-03-11")  # today still 30
    assert dl.resolve(30, 80, "2027-03-11", "2027-03-10").amount == 30 and dl.resolve(30, 80, "2027-03-11", "2027-03-10").pending_amount == 80
    assert dl.resolve(30, 80, "2027-03-11", "2027-03-11").amount == 80 and dl.resolve(30, 80, "2027-03-11", "2027-03-12").pending_amount is None
    assert dl.set_amount(a, 20, today_key="2027-03-10", next_day_key="2027-03-11") == "set"  # lowering again cancels the pending raise
    assert (a.daily_loss_limit_usd, a.daily_loss_limit_pending_usd) == (20, None)
    dl.set_amount(a, 90, today_key="2027-03-10", next_day_key="2027-03-11")
    assert dl.set_amount(a, 20, today_key="2027-03-10", next_day_key="2027-03-11") == "unchanged" and a.daily_loss_limit_pending_usd is None  # back to the amount in force: the raise is cancelled
    dl.set_amount(a, 90, today_key="2027-03-10", next_day_key="2027-03-11")
    assert dl.apply_pending(a, "2027-03-10") is False and a.daily_loss_limit_usd == 20  # not due
    assert dl.apply_pending(a, "2027-03-11") is True and a.daily_loss_limit_usd == 90 and a.daily_loss_limit_pending_usd is None and a.daily_loss_limit_pending_from is None


def test_setting_the_same_amount_cancels_a_pending_raise_and_clears_the_default_flag():
    a = acct(daily_loss_limit_usd=40.0, daily_loss_limit_is_default=True)
    dl.set_amount(a, 90, today_key="2027-03-10", next_day_key="2027-03-11")
    assert a.daily_loss_limit_is_default is False  # the customer has chosen
    assert dl.set_amount(a, 40, today_key="2027-03-10", next_day_key="2027-03-11") == "unchanged" and a.daily_loss_limit_pending_usd is None


def test_the_default_is_ten_percent_of_the_last_value_else_of_the_capital():
    assert dl.default_amount(2000.0, 500.0) == 200.0 and dl.default_amount(None, 500.0) == 50.0 and dl.default_amount(0, 500.0) == 50.0
    assert dl.default_amount(None, None) is None and dl.default_amount(float("nan"), -5) is None
    assert dl.DEFAULT_PCT < rl.TOTAL_STOP_PCT and dl.SUGGESTED_PCT == 5.0


def test_the_daily_rule_in_dollars():
    h = account_loss.daily_loss_hit
    assert h(start_equity=1000, equity=960, limit_usd=40) is not None and h(start_equity=1000, equity=960.01, limit_usd=40) is None
    hit = h(start_equity=1000, equity=950, limit_usd=40)
    assert hit.loss_usd == 50 and abs(hit.loss_pct - 5.0) < 1e-9
    assert h(start_equity=1000, equity=1000, limit_usd=None) is not None  # no amount: fail closed


def test_the_pause_message_has_the_amount_lost_and_the_total_so_far():
    m = account_loss.pause_message(loss_usd=52.5, reference_equity=1000.0, equity=900.0)
    assert "You lost 52.50 USDT today" in m and "No new trades until midnight Lagos time" in m and "Total loss so far: 100.00 USDT against your reference value of 1000.00 USDT" in m
    assert "Total loss so far" not in account_loss.pause_message(loss_usd=52.5, reference_equity=None, equity=900.0)  # no reference known: that clause is left out


def test_the_total_stop_is_one_constant_for_every_account():
    assert rl.TOTAL_STOP_PCT == 50.0 and rl.total_loss_threshold(1) == rl.total_loss_threshold(40) == 50.0
    assert f"{rl.TOTAL_STOP_PCT:g}%" in account_loss.CUSTOMER_MESSAGE_TOTAL_STOP


# ------------------------------------------------------------------ the one-time default
def _live_account(email, *, capital=1000.0, live=True):
    st = make_settings()
    aid = new_account(st, email=email, allocated_capital=capital, daily_loss_limit_usd=None)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        if not live:
            a.live_trading_enabled_at = None
    return aid


def test_the_default_for_existing_live_accounts_runs_once_and_touches_nothing_else(db, caplog):
    stored = _live_account("a@example.com", capital=500.0)
    capital_only = _live_account("b@example.com", capital=300.0)
    nothing = _live_account("c@example.com", capital=None)
    practice = _live_account("d@example.com", capital=1000.0, live=False)
    has_one = new_account(make_settings(), email="e@example.com", daily_loss_limit_usd=77.0)
    with session_scope() as s:
        s.add(AccountDayEquity(account_id=stored, day_key="2027-03-09", start_equity=1900.0, last_equity=2000.0, last_equity_at=NOON, approximate=False))
        s.add(AccountDayEquity(account_id=stored, day_key="2027-03-08", start_equity=900.0, last_equity=100.0, last_equity_at=NOON, approximate=False))  # older: not the last
    with caplog.at_level("WARNING"):
        with session_scope() as s:
            assert dl.ensure_defaults(s) == 2
    with session_scope() as s:
        get = lambda i: s.get(TradingAccount, i)
        assert get(stored).daily_loss_limit_usd == 200.0 and get(stored).daily_loss_limit_is_default is True  # 10% of the last stored value, not of the capital
        assert get(capital_only).daily_loss_limit_usd == 30.0 and get(capital_only).daily_loss_limit_is_default is True  # the fallback
        assert get(nothing).daily_loss_limit_usd is None  # no number to base it on: it stays blocked until the customer sets one
        assert get(practice).daily_loss_limit_usd is None  # not live-authorised
        assert get(has_one).daily_loss_limit_usd == 77.0 and get(has_one).daily_loss_limit_is_default is False
    assert len([r for r in caplog.records if "default of" in r.getMessage()]) == 2 and "a@example.com" not in caplog.text  # logged once per account, no personal data
    with session_scope() as s:
        assert dl.ensure_defaults(s) == 0  # a second run changes nothing
        s.get(TradingAccount, stored).daily_loss_limit_usd = 150.0
    with session_scope() as s:
        dl.ensure_defaults(s)
        assert s.get(TradingAccount, stored).daily_loss_limit_usd == 150.0  # a customer's choice is never overwritten


def test_the_startup_step_never_stops_the_service(monkeypatch, caplog):
    def boom(session):
        raise RuntimeError("no such column")

    monkeypatch.setattr(dl, "ensure_defaults", boom)
    with caplog.at_level("WARNING"):
        assert dl.run_startup_default() == 0
    assert "did not run (RuntimeError)" in caplog.text and "no such column" not in caplog.text  # the class name only


def test_the_app_and_the_scheduler_both_run_the_step():
    assert "daily_limit.run_startup_default()" in (ROOT / "src/mba_bot/api/app.py").read_text() and "daily_limit.run_startup_default()" in (ROOT / "src/mba_bot/main.py").read_text()


# ------------------------------------------------------------------ the rule in the real cycle
def test_a_loss_under_the_amount_does_not_block_and_at_the_amount_blocks(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    run_cycle(aid, st)
    client.free["USDT"] = 960.5  # down 39.5, the amount is 40
    client._ohlcv = BULLISH_OHLCV
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)
    assert rows(AccountLossPause) == [] and len(all_trades(aid)) == 1


def test_a_loss_of_exactly_the_amount_blocks_and_writes_the_amount_used(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    run_cycle(aid, st)
    client.free["USDT"] = 960.0  # exactly 40 below the day start of 1000
    client._ohlcv = BULLISH_OHLCV
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)
    pauses = rows(AccountLossPause)
    assert len(pauses) == 1 and pauses[0].limit_usd == 40.0 and all_trades(aid) == [] and live_orders(client) == []


def test_a_raise_starts_at_midnight_in_the_real_cycle_and_a_lowering_acts_at_once(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        dl.set_amount(a, 100.0, today_key="2027-03-10", next_day_key="2027-03-11")  # a raise from 40: pending
        assert (a.daily_loss_limit_usd, a.daily_loss_limit_pending_usd) == (40.0, 100.0)
    run_cycle(aid, st)
    client.free["USDT"] = 950.0  # down 50 today: over the 40 in force, under the pending 100
    client._ohlcv = BULLISH_OHLCV
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)
    assert len(rows(AccountLossPause)) == 1 and rows(AccountLossPause)[0].limit_usd == 40.0 and all_trades(aid) == []  # today the old, lower amount holds
    clock.instant = datetime(2027, 3, 10, 23, 30, tzinfo=U)  # 00:30 Lagos on the 11th
    run_cycle(aid, st)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        assert (a.daily_loss_limit_usd, a.daily_loss_limit_pending_usd, a.daily_loss_limit_pending_from) == (100.0, None, None)  # promoted at the first check of the new day
    client.free["USDT"] = 910.0  # down 40 from the new day's start (950): under the new 100
    clock.instant += timedelta(minutes=10)
    run_cycle(aid, st)
    assert len(rows(AccountLossPause)) == 1  # no second pause: 40 is under 100


def test_a_live_account_with_no_amount_takes_no_new_entry_when_the_switch_is_on(db, monkeypatch):
    st = live_settings()
    client, clock, aid = rig(monkeypatch, st)
    with session_scope() as s:
        s.get(TradingAccount, aid).daily_loss_limit_usd = None
    client._ohlcv = BULLISH_OHLCV
    n = FakeNotifier()
    run_cycle(aid, st, n)
    assert all_trades(aid) == [] and live_orders(client) == [] and rows(AccountLossPause) == []  # no order was sent
    from test_account_daily_loss import reasons

    assert account_loss.R_LIMIT_MISSING in reasons()
    assert any("set a daily loss limit" in t or "not set a daily loss limit" in t for t in n.texts)


def test_with_the_switch_off_nothing_is_required_or_read(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, trade_telemetry=True)
    client, clock, aid = rig(monkeypatch, st)
    with session_scope() as s:
        s.get(TradingAccount, aid).daily_loss_limit_usd = None
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1 and rows(AccountDayEquity) == []


# ------------------------------------------------------------------ the API
def _client_with_account(email, *, capital=1000.0):
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    _signup_with_account(client, email)
    if capital is not None:
        assert client.patch("/api/account", json={"allocated_capital": capital}).status_code == 200
    return client


def test_enable_live_trading_and_start_bot_are_refused_without_the_amount():
    email = "needs-limit@example.com"
    client = _client_with_account(email)
    try:
        _mark_paid(email)
        _retag_mainnet(client)
        _make_eligible(email)
        r = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert r.status_code == 409 and "daily loss limit" in r.json()["detail"]
        assert client.get("/api/account").json()["live_trading_enabled_at"] is None  # nothing was switched on
        assert client.patch("/api/account", json={"daily_loss_limit_usd": 50}).status_code == 200
        assert client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True}).status_code == 200
        # Start Bot has its own check: clear the amount in the database (as an account from before this change would have) and it is refused there too
        with session_scope() as s:
            from mba_bot.models import Customer

            cid = s.query(Customer).filter(Customer.email == email).one().id
            s.query(TradingAccount).filter(TradingAccount.customer_id == cid).one().daily_loss_limit_usd = None
        refused = client.post("/api/account/start-bot", json={"confirm": True})
        assert refused.status_code == 409 and "daily loss limit" in refused.json()["detail"] and client.get("/api/account").json()["bot_started_at"] is None
        assert client.patch("/api/account", json={"daily_loss_limit_usd": 50}).status_code == 200
        assert client.post("/api/account/start-bot", json={"confirm": True}).status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_the_amount_is_validated_against_the_total_stop_level_and_zero():
    client = _client_with_account("validate-limit@example.com", capital=1000.0)
    try:
        for bad in (0, -5):
            assert client.patch("/api/account", json={"daily_loss_limit_usd": bad}).status_code == 422
        too_high = client.patch("/api/account", json={"daily_loss_limit_usd": 500})
        assert too_high.status_code == 422 and "500.00" in too_high.json()["detail"] and client.get("/api/account").json()["daily_loss_limit_usd"] is None  # nothing was saved
        ok = client.patch("/api/account", json={"daily_loss_limit_usd": 499})
        assert ok.status_code == 200 and ok.json()["daily_loss_limit_usd"] == 499
    finally:
        client.__exit__(None, None, None)


def test_the_screen_numbers_and_the_raise_notice_come_from_the_server():
    client = _client_with_account("screen-limit@example.com", capital=1000.0)
    try:
        before = client.get("/api/account").json()
        assert before["daily_loss_limit_usd"] is None and before["daily_loss_limit_required"] is True
        assert before["daily_loss_limit_suggested_usd"] == 50.0 and before["daily_loss_limit_max_usd"] == 500.0 and before["account_value_reference"] == 1000.0
        first = client.patch("/api/account", json={"daily_loss_limit_usd": 40}).json()
        assert first["daily_loss_limit_usd"] == 40 and first["daily_loss_limit_pct"] == 4.0 and first["daily_loss_limit_required"] is False and first["daily_loss_limit_is_default"] is False
        raised = client.patch("/api/account", json={"daily_loss_limit_usd": 90}).json()
        assert raised["daily_loss_limit_usd"] == 40 and raised["daily_loss_limit_pending_usd"] == 90 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raised["daily_loss_limit_pending_from"])  # the screen says when it starts
        lowered = client.patch("/api/account", json={"daily_loss_limit_usd": 20}).json()
        assert lowered["daily_loss_limit_usd"] == 20 and lowered["daily_loss_limit_pending_usd"] is None  # at once, and the pending raise is gone
        assert lowered["total_loss_stop_threshold_pct"] == 50.0
    finally:
        client.__exit__(None, None, None)


def test_the_one_trade_warning_follows_risk_capital_and_amount():
    client = _client_with_account("warn-limit@example.com", capital=1000.0)
    try:
        client.patch("/api/account", json={"daily_loss_limit_usd": 40, "risk_pct": 1.0})
        assert client.get("/api/account").json()["one_trade_can_exceed_limit"] is False  # 1% of 1000 = 10 <= 40
        r = client.patch("/api/account", json={"risk_pct": 2.0}).json()
        assert r["one_trade_can_exceed_limit"] is False  # 20 <= 40
    finally:
        client.__exit__(None, None, None)
    assert dl.one_trade_can_exceed(5.0, 1000.0, 40.0) is True  # the 5% case needs the raised ceiling and an acknowledgment, so it is proven on the pure rule


# ------------------------------------------------------------------ data and code
def test_the_migration_is_additive_idempotent_and_reversible(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "d2a5b9c7e1f4")
    eng = lambda: inspect(create_engine(f"sqlite:///{path}"))
    acct_cols = lambda: {c["name"] for c in eng().get_columns("trading_accounts")}
    pause_cols = lambda: {c["name"] for c in eng().get_columns("account_loss_pauses")}
    before_a, before_p = acct_cols(), pause_cols()
    command.upgrade(cfg, "e3b6c8a1d5f2")
    new = {"daily_loss_limit_usd", "daily_loss_limit_pending_usd", "daily_loss_limit_pending_from", "daily_loss_limit_is_default"}
    assert acct_cols() == before_a | new and pause_cols() == before_p | {"limit_usd"}
    command.upgrade(cfg, "e3b6c8a1d5f2")  # idempotent
    command.downgrade(cfg, "d2a5b9c7e1f4")
    assert acct_cols() == before_a and pause_cols() == before_p
    command.upgrade(cfg, "head")
    assert new <= acct_cols()


def test_nothing_reads_the_old_percent_variable():
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text()
        assert "account_daily_loss_pct" not in text, path
        if path.name == "config.py":
            assert 'os.environ.get("ACCOUNT_DAILY_LOSS_PCT")' not in text and '"ACCOUNT_DAILY_LOSS_PCT"' not in text
    example = (ROOT / ".env.example").read_text()
    assert "ACCOUNT_DAILY_LOSS_LIMIT" in example and "# ACCOUNT_DAILY_LOSS_PCT" not in example
