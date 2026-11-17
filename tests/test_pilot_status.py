"""PR 2, python -m mba_bot.pilot_status: the read-only one-screen-per-live-account status for the owner.

PROVES (fake exchange, scratch SQLite, frozen clock): the screen shows mode, switches, equity and day start, the daily pause and the total loss stop, the open trade, the last signal and error and the last 10
decisions; it makes no exchange call, writes nothing (file bytes unchanged, a write attempt fails), creates no missing database, skips practice accounts, and prints no key, secret, e-mail or full account id.
DOES NOT PROVE: that the numbers match Binance (equity is the last stored sample)."""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta

import pytest
from helpers_live import BULLISH_OHLCV, FakeNotifier, all_trades, install, make_settings, new_account, open_protected, run_cycle, spot_client
from sqlalchemy import text
from test_account_daily_loss import NOON, rig

from mba_bot import exchange, pilot_status
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, TradingAccount
from mba_bot.restart_gate import _engine


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def env(monkeypatch, db, **extra):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}/test.db")
    monkeypatch.setenv("ENCRYPTION_KEY", "x" * 44)
    for k, v in extra.items():
        monkeypatch.setenv(k, str(v))
    return f"sqlite:///{db}/test.db"


def live_state(monkeypatch, **over):
    st = make_settings(max_notional_multiplier=100.0, trade_telemetry=True, account_daily_loss_limit=True, account_total_loss_stop=True, protection_precheck_mode="off", **over)
    client, clock, aid = rig(monkeypatch, st, market="futures", email="owner-pilot@example.com")
    client.total_margin_balance = 1000.0
    return st, client, clock, aid


def screen(monkeypatch, db, **extra):
    url = env(monkeypatch, db, **extra)
    return pilot_status.render(url, now=NOON + timedelta(minutes=30))


# ------------------------------------------------------------------ content
def test_the_screen_shows_the_state_of_a_live_account(db, monkeypatch):
    st, client, clock, aid = live_state(monkeypatch)
    run_cycle(aid, st)  # first sample 1000
    client.total_margin_balance = 950.0
    client._ohlcv = BULLISH_OHLCV
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)  # down 50 USDT: the daily limit (40) acts, the 50% total stop does not
    lines, code = screen(monkeypatch, db, ACCOUNT_DAILY_LOSS_LIMIT=1, ACCOUNT_TOTAL_LOSS_STOP=1, FUTURES_LEVERAGE_MODE="size_cap", PROTECTION_PRECHECK_MODE="enforce")
    text_ = "\n".join(lines)
    assert code == 0 and f"=== account {aid[:8]} | LIVE | futures | BTC/USDT (manual) ===" in text_
    assert "switches on:" in text_ and "ACCOUNT_DAILY_LOSS_LIMIT=True" in text_ and "ACCOUNT_TOTAL_LOSS_STOP=True" in text_ and "FUTURES_LEVERAGE_MODE=size_cap" in text_ and "PROTECTION_PRECHECK_MODE=enforce" in text_
    assert "kill switch:" in text_
    assert "equity: last sample 950.00" in text_ and "start 1000.00" in text_ and "-5.00%" in text_
    assert "daily pause: ACTIVE" in text_ and "limit 40.00 USDT" in text_
    assert "total loss stop: not active; reference 1000.00" in text_ and "limit 50% below it" in text_
    assert "open trade: none" in text_ and "last signal" in text_ and "last error:" in text_
    assert "account_daily_loss_pause" in text_  # among the last decisions


def test_the_total_loss_stop_and_the_open_trade_show(db, monkeypatch):
    st, client, clock, aid = live_state(monkeypatch)
    run_cycle(aid, st)
    client.total_margin_balance = 450.0
    clock.instant = NOON + timedelta(minutes=5)
    run_cycle(aid, st)
    lines, _ = screen(monkeypatch, db, ACCOUNT_DAILY_LOSS_LIMIT=1, ACCOUNT_TOTAL_LOSS_STOP=1)
    assert "total loss stop: ACTIVE" in "\n".join(lines) and "only the customer's reset on the dashboard releases it" in "\n".join(lines)


def test_an_open_trade_is_listed(db, monkeypatch):
    client, base, aid = open_protected(monkeypatch, market="futures")
    lines, _ = pilot_status.render(env(monkeypatch, db), now=base_now())
    t = "\n".join(lines)
    assert "open trade: BTC/USDT long open (protection protected)" in t and "strategy ema_cross_atr_v1" in t


def base_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc) + timedelta(minutes=1)


def test_the_last_ten_decisions_newest_first(db, monkeypatch):
    st, client, clock, aid = live_state(monkeypatch)
    run_cycle(aid, st)
    with session_scope() as s:
        for i in range(14):
            s.add(DecisionLog(account_id=aid, ts=NOON + timedelta(minutes=i), mode="live", market_type="futures", pair="BTC/USDT", strategy_id="ema_cross_atr_v1", stage="entry", outcome="skipped",
                              reason_code=f"test_reason_{i:02d}"))
    lines, _ = screen(monkeypatch, db)
    rows = [l for l in lines if "test_reason_" in l]
    assert len(rows) == 10 and "test_reason_13" in rows[0] and "test_reason_04" in rows[-1] and not any("test_reason_03" in l for l in lines)


def test_switches_that_are_off_are_listed_as_off(db, monkeypatch):
    st, client, clock, aid = live_state(monkeypatch)
    run_cycle(aid, st)
    for name in ("ACCOUNT_DAILY_LOSS_PCT", "ACCOUNT_TOTAL_LOSS_STOP", "FUTURES_LEVERAGE_MODE", "FOREIGN_ORDER_ALARM", "PROTECTION_PRECHECK_MODE"):
        monkeypatch.delenv(name, raising=False)
    lines, _ = screen(monkeypatch, db)
    off = next(l for l in lines if l.startswith("switches off:"))
    assert "ACCOUNT_TOTAL_LOSS_STOP=False" in off and "FOREIGN_ORDER_ALARM=off" in off and "FUTURES_LEVERAGE_MODE=strict" in off


# ------------------------------------------------------------------ read-only, no secrets, exit codes
def test_it_is_read_only_makes_no_exchange_call_and_prints_no_secret(db, monkeypatch):
    st, client, clock, aid = live_state(monkeypatch)
    run_cycle(aid, st)
    path = db / "test.db"
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    def boom(**kw):
        raise AssertionError("pilot_status must not build an exchange client")

    monkeypatch.setattr(exchange, "build_exchange_client", boom)
    lines, code = screen(monkeypatch, db)
    out = "\n".join(lines)
    assert code == 0 and hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert aid not in out and "@" not in out and "owner-pilot" not in out and "synthetic" not in out.lower()  # only the first 8 characters of the id
    assert not re.search(r"[A-Za-z0-9+/=_-]{40,}", out)  # nothing that looks like a key or a token
    engine = _engine(f"sqlite:///{path}")
    with pytest.raises(Exception, match="readonly|read-only"):
        with engine.begin() as c:
            c.execute(text("DELETE FROM trading_accounts"))
    engine.dispose()


def test_practice_accounts_are_skipped_and_no_live_account_exits_3(db, monkeypatch):
    st = make_settings()
    new_account(st)
    with session_scope() as s:
        s.query(TradingAccount).one().bot_started_at = None
    lines, code = screen(monkeypatch, db)
    assert code == 3 and not any(l.startswith("=== account") for l in lines)


def test_a_missing_database_is_exit_2_with_a_class_name_only(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/nope.db")
    monkeypatch.setenv("ENCRYPTION_KEY", "x" * 44)
    assert pilot_status.main([]) == 2
    err = capsys.readouterr().err
    assert "FileNotFoundError" in err and "nope.db" not in err and not (tmp_path / "nope.db").exists()


def test_a_main_run_prints_the_screen(db, monkeypatch, capsys):
    st, client, clock, aid = live_state(monkeypatch)
    run_cycle(aid, st)
    env(monkeypatch, db)
    code = pilot_status.main([])
    assert code == 0 and f"=== account {aid[:8]}" in capsys.readouterr().out


def test_the_screen_reads_through_the_query_only_engine(db, monkeypatch):
    """A writable connection would also print the same screen, so pin the engine: every connection the tool uses must refuse a write."""
    env(monkeypatch, db)
    used = []
    real = pilot_status._engine

    def spy(url):
        engine = real(url)
        used.append(engine)
        return engine

    monkeypatch.setattr(pilot_status, "_engine", spy)
    pilot_status.render(f"sqlite:///{db}/test.db")
    assert len(used) == 1
    with used[0].connect() as con:
        with pytest.raises(Exception, match="readonly|read-only|query_only|attempt to write"):
            con.execute(text("CREATE TABLE should_not_exist (x integer)"))
