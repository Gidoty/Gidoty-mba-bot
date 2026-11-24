"""Customer status line and the round watchdog (docs/safety/health_and_status.md). Fake everything: isolated database, a fake alert sender, no systemd, no network.

PROVES: STATUS_LINE is off by default (404); the sentence for each state, in the order of who is in charge (paused, Telegram, not started, safety check, kill switch, total stop, daily limit, problem, never checked,
behind, open position, running), with no internal text, error text, address or secret; the staleness limit follows the poll interval; the route reads the customer's own account only and changes nothing; the watchdog
reads the database read-only (a missing file is not created), is silent when healthy or not armed, says the service is down (with the number of open live trades) or that rounds are stale or the database unreadable,
repeats at most hourly, says once when it clears, and puts nothing but fixed text and counts in a message. DOES NOT PROVE systemd, Telegram or cron behaviour (UNVERIFIED)."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test_health.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from mba_bot import db as dbmod, status_line as SL  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.deps import get_settings  # noqa: E402
from mba_bot.config import load_settings  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, TradingAccount  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
_n = iter(range(1, 99999))


def facts(**kw):
    base = dict(runtime_status="active", needs_telegram=False, last_scanned_at=NOW - timedelta(minutes=2), last_check_ok=True, poll_interval_seconds=300, kill_engaged=False, daily_loss_pause=False,
                total_loss_stop=False, open_position=False)
    base.update(kw)
    return SL.Facts(**base)


def line(**kw):
    return SL.build(facts(**kw), NOW)


# ---------------------------------------------------------------- the sentence
def test_a_healthy_bot_says_it_is_running_and_when_it_last_checked():
    r = line()
    assert r["state"] == "running" and r["headline"] == "Running." and "every 5 minutes" in r["detail"] and r["last_check"] == "2 minutes ago" and r["last_check_ok"] is True
    assert line(open_position=True, runtime_status="trade_open")["headline"] == "Running. A position is open and being watched."


@pytest.mark.parametrize("kw,state,needle", [
    (dict(runtime_status="paused"), "paused", "paused"),
    (dict(needs_telegram=True), "waiting", "Telegram link"),
    (dict(runtime_status="not_started"), "waiting", "Start Bot"),
    (dict(runtime_status="attention"), "on_hold", "safety check"),
    (dict(kill_engaged=True), "on_hold", "paused by the service operator"),
    (dict(total_loss_stop=True), "on_hold", "total loss stop"),
    (dict(daily_loss_pause=True), "on_hold", "daily loss limit"),
    (dict(runtime_status="error", last_check_ok=False), "problem", "problem"),
    (dict(last_scanned_at=None), "waiting", "has not checked the market yet"),
    (dict(last_scanned_at=NOW - timedelta(minutes=40)), "delayed", "since 40 minutes ago"),
])
def test_each_state_has_its_own_plain_sentence(kw, state, needle):
    r = line(**kw)
    assert r["state"] == state and needle in (r["headline"] + (r["detail"] or ""))


def test_a_failed_last_check_is_a_problem_even_when_the_account_screen_says_active():
    r = line(runtime_status="active", last_check_ok=False)
    assert r["state"] == "problem" and r["last_check_ok"] is False


def test_the_order_of_who_is_in_charge():
    assert line(runtime_status="paused", kill_engaged=True, daily_loss_pause=True)["state"] == "paused"
    assert "Telegram" in line(needs_telegram=True, kill_engaged=True)["headline"]
    assert "safety check" in line(runtime_status="attention", kill_engaged=True)["headline"]
    assert "service operator" in line(kill_engaged=True, total_loss_stop=True, daily_loss_pause=True)["headline"]
    assert "total loss stop" in line(total_loss_stop=True, daily_loss_pause=True)["headline"]
    assert line(daily_loss_pause=True, runtime_status="error", last_check_ok=False)["state"] == "on_hold"
    assert line(last_scanned_at=NOW - timedelta(hours=3), runtime_status="error", last_check_ok=False)["state"] == "problem"


def test_an_open_position_is_reassured_where_new_trades_are_stopped():
    for kw in (dict(kill_engaged=True), dict(total_loss_stop=True), dict(daily_loss_pause=True), dict(last_scanned_at=NOW - timedelta(hours=1)), dict(runtime_status="error", last_check_ok=False)):
        r = line(open_position=True, **kw)
        assert "stop-loss" in ((r["detail"] or "") + r["headline"]), kw
        assert "stop-loss" not in ((line(**kw)["detail"] or "") + line(**kw)["headline"]), kw


def test_the_staleness_limit_follows_the_poll_interval():
    assert SL.stale_after(300) == 15 * 60 and SL.stale_after(3600) == 3 * 3600 and SL.stale_after(10) == 15 * 60
    assert line(poll_interval_seconds=3600, last_scanned_at=NOW - timedelta(hours=2))["state"] == "running"
    assert line(poll_interval_seconds=3600, last_scanned_at=NOW - timedelta(hours=4))["state"] == "delayed"
    assert line(last_scanned_at=NOW - timedelta(minutes=15))["state"] == "running" and line(last_scanned_at=NOW - timedelta(minutes=16))["state"] == "delayed"


def test_times_are_plain_words():
    assert [SL.ago(s) for s in (5, 89, 120, 3000, 7200, 200000)] == ["just now", "just now", "2 minutes ago", "50 minutes ago", "2 hours ago", "2 days ago"]


def test_nothing_internal_appears_in_any_sentence():
    banned = ("error", "exception", "traceback", "http", "binance", "api key", "sqlite", "last_error", "runtime_status", "is_active")
    for kw in (dict(), dict(runtime_status="error", last_check_ok=False), dict(kill_engaged=True), dict(runtime_status="attention"), dict(last_scanned_at=None), dict(last_scanned_at=NOW - timedelta(days=2))):
        r = line(**kw)
        text = json.dumps(r).lower()
        assert not [b for b in banned if b in text.replace("last_check_ok", "")], (kw, text)
        assert set(r) == {"state", "headline", "detail", "last_check_at", "last_check", "last_check_ok"}


def test_a_naive_time_is_read_as_utc():
    r = SL.build(facts(last_scanned_at=datetime(2026, 11, 24, 11, 58)), NOW)
    assert r["last_check"] == "just now" or r["last_check"] == "2 minutes ago"


# ---------------------------------------------------------------- the route
def _override(**kw):
    app.dependency_overrides[get_settings] = lambda: dataclasses.replace(load_settings(), **kw)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    import asyncio

    from mba_bot.api import app as appmod

    async def idle(settings):  # the real scheduler would run a round at start-up and write its own last_scanned_at and error
        await asyncio.sleep(3600)

    monkeypatch.setattr(appmod, "run_forever", idle)
    yield
    app.dependency_overrides.pop(get_settings, None)


def _signup(client):
    email = f"hs{next(_n)}@example.com"
    assert client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True}).status_code == 201
    assert client.post("/api/account", json={"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "api_key": "k", "api_secret": "s"}).status_code == 201
    return email


def test_the_route_is_off_by_default():
    assert load_settings().status_line is False
    with TestClient(app, base_url="https://testserver") as c:
        _signup(c)
        assert c.get("/api/account/status-line").status_code == 404


def test_the_route_answers_for_the_signed_in_customer_only_and_changes_nothing():
    _override(status_line=True)
    with TestClient(app, base_url="https://testserver") as c:
        email = _signup(c)
        r = c.get("/api/account/status-line")
        assert r.status_code == 200 and r.json()["state"] == "waiting" and "Telegram" in r.json()["headline"]  # a new account has no linked chat
        with session_scope() as s:
            a = s.query(TradingAccount).join(Customer).filter(Customer.email == email).one()
            a.notification_target, a.last_scanned_at = "123", datetime.now(timezone.utc) - timedelta(minutes=1)
        with session_scope() as s:
            a = s.query(TradingAccount).join(Customer).filter(Customer.email == email).one()
            before = {k.name: getattr(a, k.name) for k in TradingAccount.__table__.columns if k.name != "updated_at"}
        r = c.get("/api/account/status-line").json()
        assert r["state"] == "running" and r["last_check"] == "just now" and "123" not in json.dumps(r)
        with session_scope() as s:
            a = s.query(TradingAccount).join(Customer).filter(Customer.email == email).one()
            assert {k.name: getattr(a, k.name) for k in TradingAccount.__table__.columns if k.name != "updated_at"} == before
            a.is_active = False
        assert c.get("/api/account/status-line").json()["state"] == "paused"
    with TestClient(app, base_url="https://testserver") as anon:
        assert anon.get("/api/account/status-line").status_code == 401


def test_the_kill_switch_and_a_stale_scan_show_up_in_the_route():
    from mba_bot import kill_switch

    _override(status_line=True)
    with TestClient(app, base_url="https://testserver") as c:
        email = _signup(c)
        with session_scope() as s:
            a = s.query(TradingAccount).join(Customer).filter(Customer.email == email).one()
            a.notification_target, a.last_scanned_at = "1", datetime.now(timezone.utc) - timedelta(hours=5)  # the poll interval is 3600 here: behind after 3 hours
        assert c.get("/api/account/status-line").json()["state"] == "delayed"
        with session_scope() as s:
            a = s.query(TradingAccount).join(Customer).filter(Customer.email == email).one()
            a.last_scanned_at = datetime.now(timezone.utc)
        assert kill_switch.set_state(dbmod._engine, engaged=True, actor="test", reason="test engage").ok
        r = c.get("/api/account/status-line").json()
        assert r["state"] == "on_hold" and "service operator" in r["headline"]
        assert kill_switch.set_state(dbmod._engine, engaged=False, actor="test", reason="test release").ok


# ---------------------------------------------------------------- the watchdog
def load_watchdog():
    spec = importlib.util.spec_from_file_location("mba_round_watchdog", ROOT / "ops" / "health" / "mba_round_watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def wd(tmp_path, monkeypatch):
    mod = load_watchdog()
    mod.sent = []
    monkeypatch.setattr(mod.alert, "send_telegram", lambda text, cfg=None: (mod.sent.append(text), True)[1])
    monkeypatch.setattr(mod.common, "log", lambda m: None)
    mod.db = tmp_path / "w.db"
    con = sqlite3.connect(mod.db)
    con.executescript("create table trading_accounts (id text, is_active integer, last_scanned_at text); create table trades (id text, is_paper integer, is_demo integer, status text);")
    con.commit()
    con.close()
    mod.state = str(tmp_path / "state")
    os.makedirs(mod.state)
    mod.write = lambda sql, *a: _write(mod.db, sql, *a)
    return mod


def _write(path, sql, *args):
    con = sqlite3.connect(path)
    con.execute(sql, args)
    con.commit()
    con.close()


T0 = datetime(2026, 11, 24, 12, 0).timestamp()


def stamp(minutes_ago):
    return (datetime.fromtimestamp(T0, timezone.utc) - timedelta(minutes=minutes_ago)).replace(tzinfo=None).isoformat(sep=" ")


def go(wd, *, now=T0, active=True):
    wd.service_active = lambda service: active
    return wd.run(now=now, db_path=str(wd.db), service="mba-bot", state=wd.state)


def test_healthy_and_not_armed_are_silent(wd):
    go(wd)
    assert wd.sent == []  # no account at all
    wd.write("insert into trading_accounts values ('a', 1, NULL)")
    go(wd)
    assert wd.sent == []  # never scanned
    wd.write("update trading_accounts set last_scanned_at = ?", stamp(3))
    go(wd)
    assert wd.sent == []
    wd.write("update trading_accounts set is_active = 0, last_scanned_at = ?", stamp(500))
    go(wd, active=False)
    assert wd.sent == []  # no active account: a stopped service is nothing to alert about


def test_a_stopped_service_is_reported_with_the_number_of_open_live_trades(wd):
    wd.write("insert into trading_accounts values ('a', 1, ?)", stamp(3))
    wd.write("insert into trades values ('t1', 0, 0, 'open')")
    wd.write("insert into trades values ('t2', 0, 0, 'closed')")
    wd.write("insert into trades values ('t3', 1, 0, 'open')")  # a practice trade does not count
    wd.write("insert into trades values ('t4', 0, 1, 'open')")  # a demo trade does not count
    go(wd, active=False)
    assert len(wd.sent) == 1 and "NOT running" in wd.sent[0] and "1 live trade(s) are open" in wd.sent[0] and "stop-losses stay on the exchange" in wd.sent[0]


def test_no_live_trade_open_is_said_plainly(wd):
    wd.write("insert into trading_accounts values ('a', 1, ?)", stamp(3))
    go(wd, active=False)
    assert "No live trade is open." in wd.sent[0]


def test_stale_rounds_with_a_running_service_are_reported_at_the_limit(wd):
    wd.write("insert into trading_accounts values ('a', 1, ?)", stamp(20))
    go(wd)
    assert wd.sent == []  # exactly at the limit is not past it
    wd.write("update trading_accounts set last_scanned_at = ?", stamp(25))
    go(wd, now=T0 + 1)
    assert len(wd.sent) == 1 and "running but no account has been checked for 25 minutes (limit 20)" in wd.sent[0]


def test_the_newest_scan_of_any_active_account_counts(wd):
    wd.write("insert into trading_accounts values ('a', 1, ?)", stamp(500))
    wd.write("insert into trading_accounts values ('b', 1, ?)", stamp(2))
    wd.write("insert into trading_accounts values ('c', 0, ?)", stamp(1))
    go(wd)
    assert wd.sent == []


def test_alerts_repeat_at_most_hourly_and_one_message_says_it_cleared(wd):
    wd.write("insert into trading_accounts values ('a', 1, ?)", stamp(60))
    go(wd)
    go(wd, now=T0 + 600)
    go(wd, now=T0 + 3000)
    assert len(wd.sent) == 1
    go(wd, now=T0 + 3700)
    assert len(wd.sent) == 2
    wd.write("update trading_accounts set last_scanned_at = ?", stamp(-70))  # a fresh scan, 70 minutes after T0
    go(wd, now=T0 + 4200)
    assert wd.sent[-1] == "MBA health watchdog: the bot is checking accounts again." and len(wd.sent) == 3
    go(wd, now=T0 + 4300)
    assert len(wd.sent) == 3


def test_a_down_service_and_stale_rounds_are_separate_conditions(wd):
    wd.write("insert into trading_accounts values ('a', 1, ?)", stamp(60))
    go(wd, active=False)
    assert "NOT running" in wd.sent[0]
    go(wd, now=T0 + 120, active=True)
    assert any("running but no account" in m for m in wd.sent) and any("checking accounts again" in m for m in wd.sent)


def test_an_unreadable_database_is_reported_and_a_missing_file_is_not_created(wd, tmp_path):
    wd.db = tmp_path / "missing.db"
    go(wd)
    assert "cannot be read" in wd.sent[0] and not wd.db.exists()
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 50)
    wd.sent.clear()
    wd.db = bad
    wd.run(now=T0, db_path=str(bad), service="mba-bot", state=str(tmp_path / "s2"))
    assert "cannot be read" in wd.sent[0]


def test_it_never_writes_to_the_database(wd):
    wd.write("insert into trading_accounts values ('a', 1, ?)", stamp(60))
    before = wd.db.read_bytes()
    go(wd)
    go(wd, active=False)
    assert wd.db.read_bytes() == before
    src = (ROOT / "ops" / "health" / "mba_round_watchdog.py").read_text()
    assert "insert " not in src.lower().replace("# ", "") and "update " not in src.lower() and "delete " not in src.lower() and "mode=ro" in src


def test_messages_hold_only_fixed_text_and_counts(wd):
    wd.write("insert into trading_accounts values ('SECRET-ACCOUNT-ID', 1, ?)", stamp(60))
    wd.write("insert into trades values ('SECRET-TRADE-ID', 0, 0, 'open')")
    go(wd)
    go(wd, now=T0 + 7200, active=False)
    text = "\n".join(wd.sent)
    assert "SECRET" not in text and str(wd.db) not in text and "/opt" not in text


def test_the_watchdog_reuses_the_backup_watchdogs_sender_and_rules():
    src = (ROOT / "ops" / "health" / "mba_round_watchdog.py").read_text()
    assert "import mba_alert as alert" in src and "import mba_common as common" in src and "alert.send_telegram" in src
    assert "requests" not in src and "urllib" not in src and "OPS_BOT_TOKEN" not in src  # no second copy of the sending code or of a secret name


def test_the_daily_pause_and_the_total_stop_reach_the_sentence_through_the_route():
    from mba_bot import account_loss
    from mba_bot.models import AccountLossPause, AccountTotalLossStop

    _override(status_line=True)
    with TestClient(app, base_url="https://testserver") as c:
        email = _signup(c)
        with session_scope() as s:
            a = s.query(TradingAccount).join(Customer).filter(Customer.email == email).one()
            a.notification_target, a.last_scanned_at = "1", datetime.now(timezone.utc)
            aid = a.id
        now = datetime.now(timezone.utc)
        tz = account_loss.zone(load_settings().account_day_timezone) or account_loss.zone("Africa/Lagos")
        with session_scope() as s:
            s.add(AccountLossPause(account_id=aid, kind=account_loss.KIND_DAILY_LOSS, day_key=account_loss.day_key(now, tz), ends_at=now + timedelta(hours=5), start_equity=100.0, equity_at_hit=90.0,
                                   loss_pct=10.0, limit_pct=5.0, timezone_name=load_settings().account_day_timezone))
        assert "daily loss limit" in c.get("/api/account/status-line").json()["headline"]
        with session_scope() as s:
            s.add(AccountTotalLossStop(account_id=aid, reference_equity=100.0, equity_at_hit=50.0, loss_pct=50.0, threshold_pct=50.0, risk_pct=1.0))
        assert "total loss stop" in c.get("/api/account/status-line").json()["headline"]
