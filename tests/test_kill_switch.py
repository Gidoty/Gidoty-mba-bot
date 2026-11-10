"""Operator kill switch (docs/safety/kill_switch.md): scope, storage, fail-closed reads with retries, the break-glass file,
the CLI, persistence across a restart, and the migration.

Everything exchange-side is the FakeBinance in tests/fake_binance.py. That proves control flow (no order is sent while
engaged, protection and exits are not gated), NOT any Binance behaviour.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from helpers_live import *  # noqa: F401,F403

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from mba_bot import config as config_mod
from mba_bot import db as db_mod
from mba_bot import killswitch as cli
from mba_bot import kill_switch, lifecycle, protection_guard, scheduler, telemetry
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, KillSwitch, KillSwitchEvent, Trade, TradingAccount
from mba_bot.venue import EntryForbidden

ROOT = Path(__file__).parent.parent
SECRET_REASON = "SENTINEL-REASON-7731"


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path}/test.db"
    init_db(url)
    telemetry.reset_for_tests()
    protection_guard.reset_for_tests()
    yield url
    scheduler._account_locks.clear()
    scheduler._stragglers.clear()
    telemetry.reset_for_tests()
    protection_guard.reset_for_tests()


def _settings(tmp_path, **over):
    base = dict(
        max_notional_multiplier=100.0,
        trade_telemetry=True,
        kill_switch_file=str(tmp_path / "KILL_SWITCH"),
        kill_switch_read_retry_seconds=0.0,
        kill_switch_read_retry_interval_seconds=0.01,
    )
    base.update(over)
    return make_settings(**base)


def _engine():
    return db_mod._engine


def _engage(reason="test engage"):
    result = kill_switch.set_state(_engine(), engaged=True, actor="test", reason=reason, retry_seconds=0.0)
    assert result.ok
    return result


def _release(reason="test release"):
    result = kill_switch.set_state(_engine(), engaged=False, actor="test", reason=reason, retry_seconds=0.0)
    assert result.ok


def _decisions(reason=None):
    with session_scope() as session:
        rows = session.query(DecisionLog).all()
    return [d for d in rows if reason is None or d.reason_code == reason]


def _detail(row):
    return json.loads(row.detail_json)


def _alerts(notifier):
    return [t for t in notifier.texts if t.startswith("MBA Bot did not open a trade")]


def _last_error(account_id):
    with session_scope() as session:
        return session.get(TradingAccount, account_id).last_error


def _events():
    with session_scope() as session:
        return session.query(KillSwitchEvent).order_by(KillSwitchEvent.at).all()


def _live(monkeypatch, settings, client, market="futures"):
    install(monkeypatch, client)
    return new_account(settings, market_type=market)


def _client(market):
    return futures_client() if market == "futures" else spot_client()


# --------------------------------------------------------------------------------------------- the default state


def test_a_fresh_database_is_seeded_released_and_seeding_is_not_repeated(db, tmp_path):
    settings = _settings(tmp_path)
    decision = kill_switch.check(_engine(), settings)
    assert not decision.engaged and decision.path == kill_switch.PATH_RELEASED and decision.attempts == 1
    assert [e.action for e in _events()] == ["seed"]
    init_db(db)  # a restart
    init_db(db)
    assert [e.action for e in _events()] == ["seed"]
    with session_scope() as session:
        assert session.query(KillSwitch).count() == 1


def test_the_telemetry_reason_code_and_settings_defaults():
    assert telemetry.R_KILL_SWITCH == "kill_switch_engaged"
    s = config_mod.load_settings()
    assert s.kill_switch_file.endswith("KILL_SWITCH") and s.kill_switch_read_retry_seconds == 3.0


def test_settings_overrides_and_clamps(monkeypatch, tmp_path):
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "X"))
    monkeypatch.setenv("KILL_SWITCH_READ_RETRY_SECONDS", "999")
    monkeypatch.setenv("KILL_SWITCH_READ_RETRY_INTERVAL_SECONDS", "0")
    s = config_mod.load_settings()
    assert s.kill_switch_file == str(tmp_path / "X")
    assert s.kill_switch_read_retry_seconds == 30.0 and s.kill_switch_read_retry_interval_seconds == 0.05


# --------------------------------------------------------------------------------------------- scope: entries


@pytest.mark.parametrize("market", ["futures", "spot"])
def test_engaged_blocks_a_live_entry_with_no_order_a_decision_row_one_notice_and_no_error(db, tmp_path, monkeypatch, market):
    settings = _settings(tmp_path)
    client = _client(market)
    account_id = _live(monkeypatch, settings, client, market)
    _engage(SECRET_REASON)

    notifier = run_cycle(account_id, settings)

    assert client.market_orders == [] and all_trades(account_id) == []
    rows = _decisions(telemetry.R_KILL_SWITCH)
    assert len(rows) == 1 and rows[0].outcome == telemetry.REJECTED and rows[0].stage == telemetry.STAGE_ENTRY
    assert _detail(rows[0])["path"] == "engaged_db" and _detail(rows[0])["check"] == "early"
    assert _alerts(notifier) == [kill_switch.CUSTOMER_MESSAGE]
    assert SECRET_REASON not in " ".join(notifier.texts) and SECRET_REASON not in rows[0].detail_json
    assert _last_error(account_id) is None  # not a fault
    notifier2 = run_cycle(account_id, settings, notifier)
    assert client.market_orders == [] and len(_alerts(notifier2)) == 1  # de-duplicated


def test_released_trades_again_after_an_engagement(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = futures_client()
    account_id = _live(monkeypatch, settings, client)
    _engage()
    run_cycle(account_id, settings)
    assert all_trades(account_id) == []
    _release()
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"


def test_paper_entries_continue_while_engaged(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path, testnet_spot_api_key="k", testnet_spot_api_secret="s")
    client = spot_client()
    account_id = _live(monkeypatch, settings, client, "spot")
    with session_scope() as session:  # make it a paper account
        account = session.get(TradingAccount, account_id)
        account.live_trading_enabled_at = None
        account.bot_started_at = None
    _engage()
    run_cycle(account_id, settings)
    trades = all_trades(account_id)
    assert len(trades) == 1 and trades[0].is_paper is True
    assert _decisions(telemetry.R_KILL_SWITCH) == []


# --------------------------------------------------------------------------------------------- scope: positions


def test_an_open_position_keeps_being_managed_and_can_exit_while_engaged(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client, settings, account_id = open_protected(monkeypatch, market="spot", settings=settings)
    _engage()
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    run_cycle(account_id, settings)  # strategy exit
    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "crossover_exit"
    run_reconcile(account_id, settings)  # recovery path also unaffected


def test_protection_is_never_gated_by_the_switch(db, tmp_path, monkeypatch):
    """The switch flips AFTER the entry's last check (the 4th read): the entry completes and is protected."""
    settings = _settings(tmp_path)
    client = futures_client()
    account_id = _live(monkeypatch, settings, client)
    real = kill_switch.acheck
    calls = {"n": 0}

    async def flips_late(source, st):
        calls["n"] += 1
        return await real(source, st) if calls["n"] <= 4 else kill_switch.Decision(True, kill_switch.PATH_DB)

    monkeypatch.setattr(kill_switch, "acheck", flips_late)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert len(client.market_orders) == 1


# --------------------------------------------------------------------------------------------- the three layers


def _scripted(monkeypatch, answers):
    """acheck answers from `answers` (True = engaged) in call order, then repeats the last."""
    seq = list(answers)

    async def fake(source, st):
        engaged = seq.pop(0) if len(seq) > 1 else seq[0]
        return kill_switch.Decision(engaged, kill_switch.PATH_DB if engaged else kill_switch.PATH_RELEASED)

    monkeypatch.setattr(kill_switch, "acheck", fake)


def test_layer_two_blocks_when_the_switch_flips_during_the_entry_checks(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = futures_client()
    account_id = _live(monkeypatch, settings, client)
    _scripted(monkeypatch, [False, False, True])  # cycle line, early, then before_order
    run_cycle(account_id, settings)
    assert client.market_orders == [] and all_trades(account_id) == []
    assert _detail(_decisions(telemetry.R_KILL_SWITCH)[0])["check"] == "before_order"


def test_layer_three_blocks_inside_open_live_trade_before_the_write_ahead_row(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = futures_client()
    account_id = _live(monkeypatch, settings, client)
    _scripted(monkeypatch, [False, False, False, True])  # ... then the check inside open_live_trade
    run_cycle(account_id, settings)
    assert client.market_orders == [] and all_trades(account_id) == []  # no pending trade row either
    assert _detail(_decisions(telemetry.R_KILL_SWITCH)[0])["check"] == "in_lifecycle"
    assert _last_error(account_id) is None


def test_open_live_trade_called_directly_raises_entry_paused_and_sends_nothing(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = futures_client()
    account_id = _live(monkeypatch, settings, client)
    _engage()

    async def go():
        with session_scope(autoflush=False) as session:
            ctx = live_context(session, account_id, client, settings, entries_allowed=True)
            await lifecycle.open_live_trade(ctx, side="long", quantity=1.0, signal_price=100.0, stop_price=90.0, risk_text="x")

    with pytest.raises(kill_switch.EntryPaused) as info:
        asyncio.run(go())
    assert isinstance(info.value, EntryForbidden) and info.value.decision.path == kill_switch.PATH_DB
    assert client.market_orders == [] and all_trades(account_id) == []


# --------------------------------------------------------------------------------------------- restarts


def test_the_state_survives_a_restart_and_so_does_a_release(db, tmp_path):
    settings = _settings(tmp_path)
    _engage("because")
    init_db(db)  # a restart: new engine, create_all, seed check
    d = kill_switch.check(_engine(), settings)
    assert d.engaged and d.path == kill_switch.PATH_DB and d.reason == "because"
    _release()
    init_db(db)
    assert not kill_switch.check(_engine(), settings).engaged


# --------------------------------------------------------------------------------------------- fail closed


def test_an_unreadable_table_fails_closed(db, tmp_path):
    settings = _settings(tmp_path)
    with _engine().begin() as conn:
        conn.execute(text("DROP TABLE kill_switch"))
    d = kill_switch.check(_engine(), settings)
    assert d.engaged and d.path == kill_switch.PATH_FC_DB and d.fail_closed


def test_a_missing_row_with_audit_events_fails_closed_and_is_not_reseeded(db, tmp_path):
    settings = _settings(tmp_path)
    with _engine().begin() as conn:
        conn.execute(text("DELETE FROM kill_switch"))
    d = kill_switch.check(_engine(), settings)
    assert d.engaged and d.path == kill_switch.PATH_FC_ROW_MISSING
    assert kill_switch.ensure_state_row(_engine()) == "anomaly"
    init_db(db)
    assert kill_switch.check(_engine(), settings).path == kill_switch.PATH_FC_ROW_MISSING


def test_a_first_run_with_empty_tables_is_seeded_released(db, tmp_path):
    settings = _settings(tmp_path)
    with _engine().begin() as conn:
        conn.execute(text("DELETE FROM kill_switch"))
        conn.execute(text("DELETE FROM kill_switch_events"))
    init_db(db)
    assert not kill_switch.check(_engine(), settings).engaged


@pytest.mark.parametrize("bad", ["7", "'yes'", "NULL"])
def test_a_malformed_value_fails_closed(db, tmp_path, bad):
    settings = _settings(tmp_path)
    with sqlite3.connect(db.replace("sqlite:///", "")) as raw:
        raw.execute("PRAGMA ignore_check_constraints = ON")
        if bad == "NULL":
            raw.execute("DROP TABLE kill_switch")
            raw.execute("CREATE TABLE kill_switch (id INTEGER PRIMARY KEY, engaged BOOLEAN, reason VARCHAR(200), changed_at DATETIME, changed_by VARCHAR(60))")
            raw.execute("INSERT INTO kill_switch (id, engaged) VALUES (1, NULL)")
        else:
            raw.execute(f"UPDATE kill_switch SET engaged = {bad} WHERE id = 1")
    d = kill_switch.check(_engine(), settings)
    assert d.engaged and d.path == kill_switch.PATH_FC_ROW_MALFORMED


def test_a_file_check_error_fails_closed_but_a_database_engagement_does_not_need_the_file(db, tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    settings = _settings(tmp_path, kill_switch_file=str(blocker / "KILL_SWITCH"))  # parent is a file: ENOTDIR, not "not found"
    d = kill_switch.check(_engine(), settings)
    assert d.engaged and d.path == kill_switch.PATH_FC_FILE
    _engage()
    assert kill_switch.check(_engine(), settings).path == kill_switch.PATH_DB  # certain without the file


# --------------------------------------------------------------------------------------------- retries (change 1)


def test_a_transient_read_error_is_retried_and_the_log_says_how_many_attempts(db, tmp_path, monkeypatch, caplog):
    settings = _settings(tmp_path, kill_switch_read_retry_seconds=2.0)
    real = kill_switch._read_db
    calls = {"n": 0}

    def flaky(engine):
        calls["n"] += 1
        return kill_switch._DbRead("error") if calls["n"] <= 2 else real(engine)

    monkeypatch.setattr(kill_switch, "_read_db", flaky)
    d = kill_switch.check(_engine(), settings, sleep=lambda s: None)
    assert not d.engaged and d.path == kill_switch.PATH_RELEASED and d.attempts == 3
    with caplog.at_level(logging.WARNING):
        kill_switch.log_decision(d, scope="acc", now=datetime.now(timezone.utc), repeat_hours=6.0)
    assert "attempts" in caplog.text and "path=released" in caplog.text and "3" in caplog.text


def test_a_persistent_read_error_fails_closed_only_after_the_retry_window_and_the_log_names_the_path(db, tmp_path, monkeypatch, caplog):
    settings = _settings(tmp_path, kill_switch_read_retry_seconds=1.0, kill_switch_read_retry_interval_seconds=0.25)
    monkeypatch.setattr(kill_switch, "_read_db", lambda engine: kill_switch._DbRead("error"))
    clock = {"t": 0.0}
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    d = kill_switch.check(_engine(), settings, sleep=sleep, monotonic=lambda: clock["t"])
    assert d.engaged and d.path == kill_switch.PATH_FC_DB and d.attempts > 1
    assert sum(slept) <= 1.0 + 1e-9 and len(slept) == d.attempts - 1  # retried within the window, then gave up
    with caplog.at_level(logging.WARNING):
        kill_switch.log_decision(d, scope="acc", now=datetime.now(timezone.utc), repeat_hours=6.0)
    assert "path=fail_closed_db_unreadable" in caplog.text and "ENGAGED" in caplog.text


def test_zero_retry_window_means_one_attempt(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path, kill_switch_read_retry_seconds=0.0)
    n = {"c": 0}

    def err(engine):
        n["c"] += 1
        return kill_switch._DbRead("error")

    monkeypatch.setattr(kill_switch, "_read_db", err)
    d = kill_switch.check(_engine(), settings)
    assert d.engaged and d.attempts == 1 and n["c"] == 1


def test_a_really_locked_database_recovers_when_the_lock_clears_within_the_window(db, tmp_path):
    settings = _settings(tmp_path, kill_switch_read_retry_seconds=3.0, kill_switch_read_retry_interval_seconds=0.05)
    path = db.replace("sqlite:///", "")
    locker = sqlite3.connect(path, isolation_level=None, timeout=0.1, check_same_thread=False)
    locker.execute("BEGIN EXCLUSIVE")
    timer = threading.Timer(0.3, lambda: (locker.execute("ROLLBACK"), locker.close()))
    timer.start()
    try:
        engine = create_engine(db, connect_args={"timeout": 0.05})
        d = kill_switch.check(engine, settings)
        engine.dispose()
    finally:
        timer.join()
    assert not d.engaged and d.path == kill_switch.PATH_RELEASED and d.attempts > 1


def test_the_async_check_retries_the_same_way(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path, kill_switch_read_retry_seconds=1.0, kill_switch_read_retry_interval_seconds=0.01)
    real = kill_switch._read_db
    calls = {"n": 0}

    def flaky(engine):
        calls["n"] += 1
        return kill_switch._DbRead("error") if calls["n"] == 1 else real(engine)

    monkeypatch.setattr(kill_switch, "_read_db", flaky)
    d = asyncio.run(kill_switch.acheck(_engine(), settings))
    assert not d.engaged and d.attempts == 2


def test_the_check_never_raises(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(kill_switch, "_evaluate", boom)
    assert kill_switch.check(_engine(), settings).engaged
    assert asyncio.run(kill_switch.acheck(_engine(), settings)).engaged


# --------------------------------------------------------------------------------------------- break-glass file


def test_the_file_engages_even_when_the_database_says_released_and_blocks_entries(db, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = futures_client()
    account_id = _live(monkeypatch, settings, client)
    Path(settings.kill_switch_file).write_text("")
    d = kill_switch.check(_engine(), settings)
    assert d.engaged and d.path == kill_switch.PATH_FILE
    run_cycle(account_id, settings)
    assert client.market_orders == [] and all_trades(account_id) == []
    assert _detail(_decisions(telemetry.R_KILL_SWITCH)[0])["path"] == "engaged_file"


def test_an_absent_file_changes_nothing_and_removing_it_alone_does_not_release_a_database_engagement(db, tmp_path):
    settings = _settings(tmp_path)
    assert not kill_switch.check(_engine(), settings).engaged
    _engage()
    Path(settings.kill_switch_file).write_text("")
    assert kill_switch.check(_engine(), settings).path == kill_switch.PATH_DB_AND_FILE
    Path(settings.kill_switch_file).unlink()
    d = kill_switch.check(_engine(), settings)
    assert d.engaged and d.path == kill_switch.PATH_DB


# --------------------------------------------------------------------------------------------- visibility


def test_an_engaged_cycle_logs_one_journal_line_naming_the_path_and_does_not_repeat_it(db, tmp_path, monkeypatch, caplog):
    settings = _settings(tmp_path)
    client = futures_client(ohlcv=FLAT_OHLCV)  # no signal: only the per-cycle visibility line is under test
    account_id = _live(monkeypatch, settings, client)
    _engage("planned maintenance")
    with caplog.at_level(logging.WARNING):
        run_cycle(account_id, settings)
        run_cycle(account_id, settings)
    lines = [r.getMessage() for r in caplog.records if "kill switch ENGAGED" in r.getMessage()]
    assert len(lines) == 1 and "path=engaged_db" in lines[0] and "planned maintenance" in lines[0]


# --------------------------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(db, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "KILL_SWITCH"))
    monkeypatch.setenv("ENCRYPTION_KEY", "SENTINEL-ENCRYPTION-KEY-000")
    monkeypatch.setenv("JWT_SECRET", "SENTINEL-JWT-SECRET-000")
    monkeypatch.setattr(cli, "_WRITE_TIMEOUT", 0.05)
    monkeypatch.setattr(cli, "_WRITE_RETRY_SECONDS", 0.3)
    return tmp_path


def _run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    assert "SENTINEL" not in out.replace(SECRET_REASON, "")
    return code, out


def test_cli_status_is_read_only(cli_env, capsys):
    before = len(_events())
    code, out = _run(capsys, "status")
    assert code == 0 and "RELEASED" in out and "path=released" in out
    assert len(_events()) == before


def test_cli_engage_writes_the_row_and_one_event_and_prints_the_exact_release_command(cli_env, capsys):
    code, out = _run(capsys, "engage", "--reason", "XRP loop")
    assert code == 0 and "ENGAGED" in out
    assert kill_switch.release_command() in out and kill_switch.status_command() in out
    assert kill_switch.release_command().endswith(' -m mba_bot.killswitch release --reason "<why>" --confirm RELEASE')
    assert "Run those from this same directory: " + os.getcwd() in out
    events = _events()
    assert [e.action for e in events] == ["seed", "engage"]
    assert events[-1].actor.startswith("cli:") and events[-1].reason == "XRP loop" and events[-1].prev_engaged is False
    assert kill_switch.check(_engine(), config_mod.load_settings()).engaged


def _clean_env(cli_env):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return env


def test_the_printed_commands_work_from_any_directory_without_the_package_installed(cli_env, tmp_path):
    """The first deploy failed with 'No module named mba_bot': the code is in src/ and not installed in the virtual
    environment. The commands the tool prints carry the interpreter and the source folder, so pasting them works."""
    env, elsewhere = _clean_env(cli_env), tmp_path / "elsewhere"
    elsewhere.mkdir()
    status = subprocess.run(kill_switch.status_command(), shell=True, cwd=elsewhere, env=env, capture_output=True, text=True)
    assert status.returncode == 0 and "kill switch: RELEASED" in status.stdout, status.stderr
    engage = subprocess.run(kill_switch.command('engage --reason "drill"'), shell=True, cwd=elsewhere, env=env, capture_output=True, text=True)
    assert engage.returncode == 0 and "ENGAGED" in engage.stdout, engage.stderr
    printed = [l for l in engage.stdout.splitlines() if l.startswith("To release it:")][0].split("To release it:", 1)[1].strip()
    release = subprocess.run(printed.replace("<why>", "drill done"), shell=True, cwd=elsewhere, env=env, capture_output=True, text=True)
    assert release.returncode == 0 and "released" in release.stdout.lower(), release.stderr
    assert not kill_switch.check(_engine(), config_mod.load_settings()).engaged


@pytest.mark.skipif(importlib.util.find_spec("mba_bot") is not None and "site-packages" in (importlib.util.find_spec("mba_bot").origin or ""),
                    reason="the package is installed in this interpreter, so the bare form cannot fail here")
def test_the_bare_form_fails_the_way_it_did_on_the_server(cli_env, tmp_path):
    out = subprocess.run(f"{shlex.quote(sys.executable)} -m mba_bot.killswitch status", shell=True, cwd=tmp_path, env=_clean_env(cli_env),
                         capture_output=True, text=True)
    assert out.returncode != 0 and "No module named" in out.stderr  # this is why the tool now prints the full form


def test_a_missing_sqlite_file_is_refused_without_creating_an_empty_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./not_here.db")
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "KILL_SWITCH"))
    for argv in (["status"], ["engage", "--reason", "x"], ["release", "--reason", "x", "--confirm", "RELEASE"]):
        code, out = _run(capsys, *argv)
        assert code == 1 and "database file not found" in out and "cd /opt/mba-bot" in out and "touch " in out
    assert not (tmp_path / "not_here.db").exists()


def test_cli_engage_without_a_reason_is_refused_and_writes_nothing(cli_env, capsys):
    code, out = _run(capsys, "engage")
    assert code == 2 and "needs --reason" in out and [e.action for e in _events()] == ["seed"]
    code, out = _run(capsys, "engage", "--reason", "   \n\t ")
    assert code == 2 and [e.action for e in _events()] == ["seed"]


def test_cli_reason_is_cleaned_and_capped(cli_env, capsys):
    _run(capsys, "engage", "--reason", "line one\nline\ttwo\x07 " + "x" * 400)
    reason = _events()[-1].reason
    assert "\n" not in reason and "\t" not in reason and "\x07" not in reason and len(reason) <= kill_switch.REASON_MAX


def test_cli_release_needs_a_reason_and_the_exact_confirmation_word(cli_env, capsys):
    _run(capsys, "engage", "--reason", "x")
    for args in (("release",), ("release", "--reason", "ok"), ("release", "--reason", "ok", "--confirm", "release"),
                 ("release", "--confirm", "RELEASE"), ("release", "--reason", "ok", "--confirm", "YES")):
        code, out = _run(capsys, *args)
        assert code == 2 and "Nothing was changed" in out, args
    assert kill_switch.check(_engine(), config_mod.load_settings()).engaged
    assert [e.action for e in _events()] == ["seed", "engage"]
    code, out = _run(capsys, "release", "--reason", "all clear", "--confirm", "RELEASE")
    assert code == 0 and "released" in out.lower()
    assert [e.action for e in _events()] == ["seed", "engage", "release"] and _events()[-1].prev_engaged is True
    assert not kill_switch.check(_engine(), config_mod.load_settings()).engaged


def test_cli_release_warns_while_the_break_glass_file_still_exists(cli_env, capsys):
    _run(capsys, "engage", "--reason", "x")
    Path(os.environ["KILL_SWITCH_FILE"]).write_text("")
    code, out = _run(capsys, "release", "--reason", "ok", "--confirm", "RELEASE")
    assert code == 0 and "WARNING" in out and "rm " + os.environ["KILL_SWITCH_FILE"] in out


def test_cli_reports_failure_when_the_database_is_locked_and_never_claims_success(cli_env, capsys, db):
    path = db.replace("sqlite:///", "")
    locker = sqlite3.connect(path, isolation_level=None, timeout=0.1)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        code, out = _run(capsys, "engage", "--reason", "x")
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert code == 1 and "database write did not succeed" in out and "touch " + os.environ["KILL_SWITCH_FILE"] in out
    assert "tables do not exist" not in out  # a locked database is not a missing table
    assert "KILL SWITCH ENGAGED in the database" not in out
    assert [e.action for e in _events()] == ["seed"]  # nothing was written


def test_cli_never_creates_tables_and_says_what_to_do_when_they_are_missing(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path}/empty.db"
    sqlite3.connect(tmp_path / "empty.db").close()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "KILL_SWITCH"))
    code, out = _run(capsys, "engage", "--reason", "x")
    assert code == 1 and "FAILED" in out and "touch " in out
    code, out = _run(capsys, "status")
    assert code == 1 and "tables do not exist" in out
    assert inspect(create_engine(url)).get_table_names() == []


def test_cli_status_on_a_locked_database_says_so_and_does_not_claim_the_tables_are_missing(cli_env, capsys, db):
    path = db.replace("sqlite:///", "")
    locker = sqlite3.connect(path, isolation_level=None, timeout=0.1)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        code, out = _run(capsys, "status")
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert code == 1 and "could not read the database" in out and "tables do not exist" not in out


def test_cli_status_shows_the_file_and_recent_events(cli_env, capsys):
    _run(capsys, "engage", "--reason", "x")
    Path(os.environ["KILL_SWITCH_FILE"]).write_text("")
    code, out = _run(capsys, "status")
    assert code == 0 and "ENGAGED" in out and "PRESENT" in out and "engage" in out and "seed" in out


def test_the_audit_log_has_exactly_one_event_per_change(cli_env, capsys):
    _run(capsys, "engage", "--reason", "a")
    _run(capsys, "engage", "--reason", "b")  # engaging twice is two audited changes, never silent
    _run(capsys, "release", "--reason", "c", "--confirm", "RELEASE")
    assert [e.action for e in _events()] == ["seed", "engage", "engage", "release"]


# --------------------------------------------------------------------------------------------- migration


def _load_migration():
    spec = importlib.util.spec_from_file_location("ks_migration", next((ROOT / "alembic" / "versions").glob("e5b9c2d7f1a4_*.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MIG = _load_migration()


def _run_mig(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _mig_state(engine):
    insp = inspect(engine)
    out = {"tables": [insp.has_table(t) for t in ("kill_switch", "kill_switch_events")]}
    if all(out["tables"]):
        with engine.connect() as c:
            out["rows"] = c.execute(text("SELECT COUNT(*), MIN(engaged) FROM kill_switch")).one()
            out["events"] = c.execute(text("SELECT COUNT(*) FROM kill_switch_events")).scalar()
    return out


def test_migration_creates_both_tables_seeds_released_once_and_repeats_safely(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/m1.db")
    _run_mig(eng, MIG.upgrade)
    first = _mig_state(eng)
    assert first["tables"] == [True, True] and tuple(first["rows"]) == (1, 0) and first["events"] == 1
    _run_mig(eng, MIG.upgrade)
    assert _mig_state(eng) == first


def test_migration_after_the_service_already_created_and_seeded_does_nothing(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/m2.db")
    from mba_bot.models import Base

    Base.metadata.create_all(eng)
    assert kill_switch.ensure_state_row(eng) == "seeded"
    before = _mig_state(eng)
    _run_mig(eng, MIG.upgrade)
    assert _mig_state(eng) == before


def test_migration_never_clears_an_engagement(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/m3.db")
    _run_mig(eng, MIG.upgrade)
    assert kill_switch.set_state(eng, engaged=True, actor="t", reason="r", retry_seconds=0.0).ok
    _run_mig(eng, MIG.upgrade)
    with eng.connect() as c:
        assert c.execute(text("SELECT engaged FROM kill_switch")).scalar() == 1


def test_migration_downgrade_removes_both_and_is_repeatable(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/m4.db")
    _run_mig(eng, MIG.upgrade)
    _run_mig(eng, MIG.downgrade)
    assert _mig_state(eng)["tables"] == [False, False]
    _run_mig(eng, MIG.downgrade)


def test_the_chain_has_one_head_and_it_follows_the_telemetry_migration():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    # The subscription billing migrations (f6c3a9d1b8e2, then a7d4e1c9b3f5) now sit on top of the kill switch one; this migration is unchanged.
    assert script.get_heads() == ["a7d4e1c9b3f5"] and MIG.down_revision == "d4a8f1c3e6b2"
