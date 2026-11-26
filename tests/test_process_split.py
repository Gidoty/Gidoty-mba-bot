"""The process split (docs/safety/process_split.md): `PROCESS_ROLE` all (default, today's single process), api, worker. Fake everything: temporary SQLite files, spies in place of the scheduler, no exchange, no systemd.

PROVES: the default and a typo are `all`, and `all` starts exactly what it started before (the scheduler with its billing jobs, the start-up default, the scheduler lock only when the switch says so); the api role runs the
billing jobs and no trading, takes its own lock and refuses to start without WAL (setting and file), on the wrong role, or when another API holds its lock; the worker refuses the api role, a missing WAL setting, a
held scheduler lock and a module that cannot import, runs no billing job, and its `--check` reports without running a round; the API and the worker can run side by side but two of either or an `all` next to a
worker cannot; preload imports the trading modules and not the web layer; the unit templates keep the two services independent (no restart of one by the other) and share one environment file.
DOES NOT PROVE anything about systemd, a real restart with a live trade, SQLite behaviour under real load, or Nginx. Those are in the rehearsal list in the document (UNVERIFIED)."""
from __future__ import annotations

import asyncio
import configparser
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from mba_bot import daily_limit, process_roles, scheduler, worker
from mba_bot.api import app as appmod
from mba_bot.config import load_settings
from mba_bot.instance_lock import AlreadyRunning, acquire_single_instance_lock

ROOT = Path(__file__).resolve().parents[1]
ENV_NAMES = ("PROCESS_ROLE", "SQLITE_WAL", "API_SINGLE_INSTANCE_LOCK", "MBA_BOT_LOCK_FILE", "MBA_API_LOCK_FILE", "SUBSCRIPTION_BILLING_ENABLED")


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for n in ENV_NAMES:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/split.db")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "mba_test_bot")
    monkeypatch.setenv("MBA_BOT_LOCK_FILE", str(tmp_path / "sched.lock"))
    monkeypatch.setenv("MBA_API_LOCK_FILE", str(tmp_path / "api.lock"))
    calls = {"run_forever": [], "billing": 0, "startup_default": 0}

    async def fake_forever(settings, **kw):
        calls["run_forever"].append(kw)
        await asyncio.sleep(3600)

    async def fake_billing(settings):
        calls["billing"] += 1
        await asyncio.sleep(3600)

    monkeypatch.setattr(appmod, "run_forever", fake_forever)
    monkeypatch.setattr(appmod, "run_billing_forever", fake_billing)
    monkeypatch.setattr(daily_limit, "run_startup_default", lambda: calls.__setitem__("startup_default", calls["startup_default"] + 1))

    class E:
        pass

    e = E()
    e.mp, e.tmp, e.calls = monkeypatch, tmp_path, calls
    e.start = lambda: TestClient(appmod.create_app(), base_url="https://testserver")
    return e


# ---------------------------------------------------------------- the setting
def test_the_default_is_the_single_process_and_a_typo_cannot_change_it(monkeypatch):
    monkeypatch.delenv("PROCESS_ROLE", raising=False)
    assert load_settings().process_role == "all"
    for raw, want in (("", "all"), ("ALL", "all"), ("apii", "all"), ("api", "api"), (" Worker ", "worker"), ("both", "all")):
        monkeypatch.setenv("PROCESS_ROLE", raw)
        assert load_settings().process_role == want, raw


def test_the_all_role_starts_exactly_what_it_started_before(env):
    env.mp.setenv("API_SINGLE_INSTANCE_LOCK", "1")
    with env.start() as c:
        assert c.get("/api/health").status_code == 200
        assert env.calls["run_forever"] == [{}] and env.calls["billing"] == 0 and env.calls["startup_default"] == 1
        assert (env.tmp / "sched.lock").exists() and not (env.tmp / "api.lock").exists()
    env.mp.delenv("API_SINGLE_INSTANCE_LOCK")
    with env.start():
        assert env.calls["run_forever"] == [{}, {}]


# ---------------------------------------------------------------- the api role
def test_the_api_role_runs_the_billing_jobs_and_no_trading_and_takes_its_own_lock(env):
    env.mp.setenv("PROCESS_ROLE", "api")
    env.mp.setenv("SQLITE_WAL", "1")
    env.mp.setenv("API_SINGLE_INSTANCE_LOCK", "1")
    with env.start() as c:
        assert c.get("/api/health").status_code == 200
        assert env.calls["run_forever"] == [] and env.calls["billing"] == 1 and env.calls["startup_default"] == 0
        assert (env.tmp / "api.lock").exists() and not (env.tmp / "sched.lock").exists()
        assert process_roles.journal_mode(load_settings().database_url) == "wal"
        assert c.post("/api/telegram/webhook", json={}).json() == {"ok": True}  # the Telegram webhook stays in the API process


def test_the_api_role_refuses_to_start_without_wal(env):
    env.mp.setenv("PROCESS_ROLE", "api")
    with pytest.raises(RuntimeError, match="SQLITE_WAL=1"):
        with env.start():
            pass
    assert env.calls["billing"] == 0


def test_the_api_role_refuses_when_the_file_is_not_in_wal_mode(env):
    import sqlite3

    path = str(env.tmp / "split.db")
    sqlite3.connect(path).execute("create table t (a)").connection.close()
    env.mp.setenv("PROCESS_ROLE", "api")
    env.mp.setenv("SQLITE_WAL", "0")
    with pytest.raises(RuntimeError):
        with env.start():
            pass
    assert process_roles.journal_mode(load_settings().database_url) in ("delete", None)


def test_the_worker_role_value_is_refused_by_the_api_process(env):
    env.mp.setenv("PROCESS_ROLE", "worker")
    env.mp.setenv("SQLITE_WAL", "1")
    with pytest.raises(RuntimeError, match="this process is the api"):
        with env.start():
            pass
    assert env.calls["run_forever"] == [] and env.calls["billing"] == 0


def test_a_second_api_is_refused_by_its_lock(env):
    env.mp.setenv("PROCESS_ROLE", "api")
    env.mp.setenv("SQLITE_WAL", "1")
    env.mp.setenv("API_SINGLE_INSTANCE_LOCK", "1")
    held = acquire_single_instance_lock(str(env.tmp / "api.lock"))
    try:
        with pytest.raises(RuntimeError, match="already holds"):
            with env.start():
                pass
    finally:
        held.close()


def test_the_api_runs_next_to_a_running_worker_but_a_single_process_does_not(env):
    env.mp.setenv("API_SINGLE_INSTANCE_LOCK", "1")
    worker_lock = acquire_single_instance_lock(str(env.tmp / "sched.lock"))
    try:
        env.mp.setenv("PROCESS_ROLE", "api")
        env.mp.setenv("SQLITE_WAL", "1")
        with env.start() as c:
            assert c.get("/api/health").status_code == 200  # side by side
        env.mp.setenv("PROCESS_ROLE", "all")
        with pytest.raises(RuntimeError, match="already holds"):
            with env.start():
                pass  # an old-style single process cannot start while the worker holds the scheduler lock
    finally:
        worker_lock.close()


# ---------------------------------------------------------------- the worker
def make_wal_db(env):
    from mba_bot import db as dbmod

    dbmod.init_db(f"sqlite:///{env.tmp}/split.db", wal=True)


def test_the_worker_checks_pass_on_a_good_setup_and_take_no_lock_for_good(env):
    env.mp.setenv("SQLITE_WAL", "1")
    make_wal_db(env)
    assert worker.main(["--check"]) == 0
    lock = acquire_single_instance_lock(str(env.tmp / "sched.lock"))  # the check released it
    lock.close()


def test_the_worker_refuses_each_unsafe_start(env, capsys):
    make_wal_db(env)
    assert worker.main(["--check"]) == 1 and "SQLITE_WAL=1" in capsys.readouterr().err  # WAL not enabled in the environment
    env.mp.setenv("SQLITE_WAL", "1")
    env.mp.setenv("PROCESS_ROLE", "api")
    assert worker.main(["--check"]) == 1 and "this process is the worker" in capsys.readouterr().err
    env.mp.delenv("PROCESS_ROLE")
    held = acquire_single_instance_lock(str(env.tmp / "sched.lock"))
    try:
        assert worker.main(["--check"]) == 1 and "already holds" in capsys.readouterr().err
    finally:
        held.close()
    env.mp.delenv("ENCRYPTION_KEY")
    assert worker.main(["--check"]) == 1 and "ENCRYPTION_KEY" in capsys.readouterr().err


def test_the_worker_check_refuses_a_file_that_is_not_in_wal_mode(env, capsys):
    import sqlite3

    c = sqlite3.connect(str(env.tmp / "split.db"))
    c.execute("create table t (a)")
    c.commit()
    c.close()
    env.mp.setenv("SQLITE_WAL", "1")
    assert worker.main(["--check"]) == 1 and "not WAL" in capsys.readouterr().err


def test_a_module_that_cannot_import_stops_the_worker(env, monkeypatch, capsys):
    env.mp.setenv("SQLITE_WAL", "1")
    make_wal_db(env)
    monkeypatch.setattr(process_roles, "preload", lambda: ["mba_bot.broken (SyntaxError)"])
    assert worker.main(["--check"]) == 1 and "mba_bot.broken (SyntaxError)" in capsys.readouterr().err


def test_the_worker_runs_the_trading_loops_without_the_billing_jobs(env):
    env.mp.setenv("SQLITE_WAL", "1")
    seen = {}

    async def fake_forever(settings, **kw):
        seen.update(kw)

    env.mp.setattr(worker, "run_forever", fake_forever)
    assert worker.main([]) == 0
    assert seen == {"billing": False} and env.calls["startup_default"] == 1
    assert process_roles.journal_mode(load_settings().database_url) == "wal"


def test_the_worker_stops_cleanly_on_a_cancel(env):
    async def forever(settings, **kw):
        await asyncio.sleep(3600)

    env.mp.setattr(worker, "run_forever", forever)

    async def go():
        t = asyncio.ensure_future(worker.amain(load_settings()))
        await asyncio.sleep(0.05)
        t.cancel()
        await asyncio.wait_for(asyncio.gather(t, return_exceptions=True), 2)

    asyncio.run(go())


def test_the_worker_has_no_web_server_in_it():
    src = (ROOT / "src" / "mba_bot" / "worker.py").read_text()
    for forbidden in ("uvicorn", "FastAPI(", "include_router", "api.app", "TestClient"):
        assert forbidden not in src


# ---------------------------------------------------------------- the scheduler pieces
def run_loop_once(monkeypatch, billing):
    calls = []

    async def run_once(settings):
        calls.append("round")

    async def rem(settings):
        calls.append("reminders")

    async def ren(settings):
        calls.append("renewal")

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "run_once", run_once)
    monkeypatch.setattr(scheduler, "_billing_reminders", rem)
    monkeypatch.setattr(scheduler, "_renewal_reconcile", ren)
    monkeypatch.setattr(scheduler.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run_poll_forever(load_settings(), **({} if billing is None else {"billing": billing})))
    return calls


def test_the_poll_loop_runs_billing_by_default_and_not_when_asked_not_to(monkeypatch):
    assert run_loop_once(monkeypatch, None) == ["round", "reminders", "renewal"]
    assert run_loop_once(monkeypatch, True) == ["round", "reminders", "renewal"]
    assert run_loop_once(monkeypatch, False) == ["round"]


def test_the_billing_loop_runs_the_billing_jobs_and_no_round(monkeypatch):
    calls = []

    async def rem(settings):
        calls.append("reminders")

    async def ren(settings):
        calls.append("renewal")

    async def run_once(settings):
        calls.append("ROUND")

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "_billing_reminders", rem)
    monkeypatch.setattr(scheduler, "_renewal_reconcile", ren)
    monkeypatch.setattr(scheduler, "run_once", run_once)
    monkeypatch.setattr(scheduler.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run_billing_forever(load_settings()))
    assert calls == ["reminders", "renewal"]


def test_run_forever_passes_the_billing_choice_to_the_poll_loop(monkeypatch):
    got = {}

    async def recover(settings, **kw):
        pass

    async def poll(settings, billing=True):
        got["billing"] = billing
        await asyncio.sleep(3600)  # a loop that returned at once would be restarted by the supervisor forever

    async def rec_loop(settings):
        await asyncio.sleep(3600)

    monkeypatch.setattr(scheduler, "recover_once", recover)
    monkeypatch.setattr(scheduler, "run_poll_forever", poll)
    monkeypatch.setattr(scheduler, "run_recovery_forever", rec_loop)

    async def go(billing):
        task = asyncio.ensure_future(scheduler.run_forever(load_settings(), billing=billing))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go(False))
    assert got == {"billing": False}


# ---------------------------------------------------------------- preload
def test_preload_imports_the_trading_modules_and_not_the_web_layer():
    code = ("import sys; from mba_bot import process_roles as p; failed = p.preload(); "
            "print(failed, 'mba_bot.billing.notices' in sys.modules, 'mba_bot.trader' in sys.modules, 'mba_bot.api.app' in sys.modules, 'mba_bot.api.routers.admin' in sys.modules)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[] True True False False"


# ---------------------------------------------------------------- the unit templates
UNITS = ROOT / "ops" / "systemd"


def unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None, allow_no_value=True)
    cp.optionxform = str
    cp.read_string((UNITS / name).read_text())
    return cp


def test_the_two_units_are_independent_and_share_one_environment_file():
    w, a = unit("mba-worker.service"), unit("mba-api.service")
    for cp in (w, a):
        assert not any(k in cp["Unit"] for k in ("PartOf", "BindsTo", "Requires"))  # restarting one unit can never restart the other
        assert "mba-api" not in cp["Unit"].get("After", "") and "mba-worker" not in cp["Unit"].get("After", "")
        assert cp["Service"]["TimeoutStopSec"] and cp["Service"]["Restart"] == "on-failure" and cp["Service"]["EnvironmentFile"] == "/opt/mba-bot/.env"
    assert "mba_bot.worker" in w["Service"]["ExecStart"] and "uvicorn mba_bot.api.app:app" in a["Service"]["ExecStart"]
    assert w["Service"]["ExecStartPre"].endswith("mba_bot.worker --check")
    assert "PROCESS_ROLE=api" in (UNITS / "mba-api.service").read_text() and "PROCESS_ROLE" not in "\n".join(l for l in (UNITS / "mba-worker.service").read_text().splitlines() if not l.startswith("#"))
    assert "SQLITE_WAL=1" in (UNITS / "mba-worker.service").read_text() and "SQLITE_WAL=1" in (UNITS / "mba-api.service").read_text()


def test_the_templates_hold_no_secret_and_the_single_unit_example_has_no_role():
    for name in ("mba-worker.service", "mba-api.service", "mba-bot.service.example"):
        text = (UNITS / name).read_text()
        assert not any(w in text.upper() for w in ("TOKEN=", "SECRET=", "KEY=", "PASSWORD="))
    single = (UNITS / "mba-bot.service.example").read_text()
    assert "PROCESS_ROLE" not in single and "mba_bot.api.app:app" in single and "TEMPLATE" in single.splitlines()[0]
    for name in ("mba-worker.service", "mba-api.service"):
        assert "NOT installed" in (UNITS / name).read_text().splitlines()[0] or "NOT INSTALLED" in (UNITS / name).read_text().splitlines()[0]
