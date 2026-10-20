"""Multi-account scheduler concurrency.

The production defect this file pins: with two or more linked accounts, the
scheduler scanned only the OLDEST and silently starved every newer one
(`last_scanned_at`, `last_signal`, `live_notified_at` stayed NULL, no error,
nothing in the journal). Cause: cycles run concurrently with one SQLite
connection each; an implicit flush (SQLAlchemy autoflush) took SQLite's
database-wide write lock and the cycle then held it across its network
`await`s; the next account's write blocked the whole event loop in sqlite3's
busy handler, so the first could never resume to release it, and the second
failed after the busy timeout; the error handler then crashed reading
`account.id` off the failed session, and `gather(return_exceptions=True)`
swallowed that.

Everything exchange-side is the stateful FakeBinance (tests/fake_binance.py)."""
from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
import time
from datetime import datetime, timezone

import ccxt.async_support as ccxt
import pytest
from helpers_live import *  # noqa: F401,F403
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from mba_bot import db as dbmod, exchange, scheduler
from mba_bot.api import app as app_module
from mba_bot.db import init_db, session_scope
from mba_bot.models import Customer, Trade, TradingAccount

PAPER_KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
# A steady trend: the EMAs never cross, so every scan concludes "no signal" (and must still be recorded).
QUIET_OHLCV = [[i, 100 + i * 0.5, 100.2 + i * 0.5, 99.8 + i * 0.5, 100 + i * 0.5, 1.0] for i in range(60)]


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    yield
    scheduler._account_locks.clear()
    scheduler._stragglers.clear()


@pytest.fixture
def fast_lock_db(tmp_path):
    """SQLite's busy timeout shrunk from 5s to 0.3s so lock contention fails fast."""
    init_db(f"sqlite:///{tmp_path}/test.db?timeout=0.3")
    yield
    scheduler._account_locks.clear()
    scheduler._stragglers.clear()


# --------------------------------------------------------------------------- helpers


def _paper(settings, email):
    account_id = new_account(settings, email=email)
    with session_scope() as session:
        session.get(TradingAccount, account_id).bot_started_at = None  # resolves to PAPER
    return account_id


def _state(account_id):
    with session_scope() as session:
        a = session.get(TradingAccount, account_id)
        return {"scanned": a.last_scanned_at, "signal": a.last_signal, "error": a.last_error, "notified": a.live_notified_at}


def _add_latency(client, seconds=0.0, hooks=()):
    """Every public coroutine of the fake takes `seconds` of 'network' time, after running `hooks(name)`."""
    def wrap(fn, name):
        async def call(*a, **k):
            for hook in hooks:
                hook(name)
            if seconds:
                await asyncio.sleep(seconds)
            return await fn(*a, **k)
        return call

    for name in dir(client):
        if not name.startswith("_") and inspect.iscoroutinefunction(getattr(client, name)):
            setattr(client, name, wrap(getattr(client, name), name))
    return client


def _use(monkeypatch, default_client, by_email=None, notifier=None):
    """Route build_exchange_client by credential (live accounts) or to `default_client` (paper)."""
    install(monkeypatch, default_client)
    keys = {f"synthetic-{email}-key": c for email, c in (by_email or {}).items()}
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kw: keys.get(kw.get("api_key"), default_client))
    notifier = notifier or FakeNotifier()
    monkeypatch.setattr(scheduler, "build_notifier", lambda *a, **k: notifier)
    return notifier


def _wrap_process_account(monkeypatch, per_account):
    real = scheduler.process_account

    async def wrapper(session, account, settings, notifier):
        handler = per_account.get(account.id)
        if handler is not None:
            return await handler(session, account, settings, notifier, real)
        return await real(session, account, settings, notifier)

    monkeypatch.setattr(scheduler, "process_account", wrapper)


class WriteLockProbe:
    """Flags a SQLite write lock that is held (an INSERT/UPDATE/DELETE executed, not yet committed or
    rolled back) at the moment a coroutine is about to suspend for I/O. That is the exact precondition of
    the original starvation."""

    def __init__(self):
        self.engine = dbmod._engine
        self.open = {}
        self.violations = []
        event.listen(self.engine, "before_cursor_execute", self._before)
        event.listen(self.engine, "commit", self._end)
        event.listen(self.engine, "rollback", self._end)
        event.listen(self.engine, "checkin", self._checkin)

    @staticmethod
    def _key(conn):
        return id(conn.connection.dbapi_connection)

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE"):
            self.open.setdefault(self._key(conn), statement.strip()[:70])

    def _end(self, conn):
        self.open.pop(self._key(conn), None)

    def _checkin(self, dbapi_connection, record):
        self.open.pop(id(dbapi_connection), None)

    def check(self, where):
        if self.open:
            self.violations.append((where, sorted(self.open.values())))

    def close(self):
        event.remove(self.engine, "before_cursor_execute", self._before)
        event.remove(self.engine, "commit", self._end)
        event.remove(self.engine, "rollback", self._end)
        event.remove(self.engine, "checkin", self._checkin)


@pytest.fixture
def probe(db):
    p = WriteLockProbe()
    yield p
    p.close()


# --------------------------------------------------------------------------- the original defect


def test_the_original_starvation_mechanism_is_real_with_implicit_flush_and_gone_without_it(fast_lock_db):
    """Not a test of our scheduler: a model of the mechanism, proving why scheduler sessions must not
    autoflush. Two overlapping cycles; the first lazy-loads a relationship after dirtying its row."""
    settings = make_settings(**PAPER_KEYS)
    ids = [new_account(settings, email=f"mech{i}@example.com") for i in range(2)]

    async def cycle(account_id, start_after, autoflush):
        await asyncio.sleep(start_after)
        with session_scope(autoflush=autoflush) as session:
            account = session.get(TradingAccount, account_id)
            account.last_scanned_at = datetime.now(timezone.utc)
            _ = account.customer.has_paid  # a query: with autoflush ON this WRITES here and takes the write lock
            await asyncio.sleep(0.2)  # "the exchange call"

    async def run(autoflush):
        return await asyncio.gather(cycle(ids[0], 0, autoflush), cycle(ids[1], 0.02, autoflush), return_exceptions=True)

    implicit = asyncio.run(run(True))
    explicit = asyncio.run(run(False))

    assert implicit[0] is None
    assert isinstance(implicit[1], OperationalError) and "database is locked" in str(implicit[1]), "the newer cycle is starved"
    assert explicit == [None, None], "with no implicit flush both cycles complete"


def test_the_probe_itself_detects_a_write_lock_held_across_a_suspension(probe):
    settings = make_settings()
    account_id = new_account(settings, email="probe@example.com")
    with session_scope() as session:  # implicit flush
        account = session.get(TradingAccount, account_id)
        account.last_scanned_at = datetime.now(timezone.utc)
        _ = account.customer.has_paid
        probe.check("implicit-flush session")
    assert probe.violations, "the probe must catch the old pattern, or it proves nothing"

    probe.violations.clear()
    with session_scope(autoflush=False) as session:
        account = session.get(TradingAccount, account_id)
        account.last_scanned_at = datetime.now(timezone.utc)
        _ = account.customer.has_paid
        probe.check("autoflush-off session")
    assert probe.violations == []


# --------------------------------------------------------------------------- every account is scanned


@pytest.mark.parametrize("older", ["paper", "live"])
def test_an_older_and_a_newer_account_are_both_scanned_and_persisted_every_round(db, monkeypatch, older):
    settings = make_settings(**PAPER_KEYS)
    client = _add_latency(spot_client(ohlcv=QUIET_OHLCV), 0.05)
    notifier = _use(monkeypatch, client)
    if older == "paper":
        first, second = _paper(settings, "paper@example.com"), new_account(settings, email="live@example.com")
        live = second
    else:
        first, second = new_account(settings, email="live@example.com"), _paper(settings, "paper@example.com")
        live = first

    started = time.monotonic()
    asyncio.run(scheduler.run_once(settings))
    elapsed = time.monotonic() - started

    for account_id in (first, second):
        s = _state(account_id)
        assert s["scanned"] is not None and s["signal"] == "none" and s["error"] is None, "a quiet market is still recorded"
    assert _state(live)["notified"] is not None
    assert sum("now live and actively scanning" in t for t in notifier.texts) == 1
    assert elapsed < 2.5, "the old defect froze the event loop for the 5s busy timeout"

    before = [_state(i)["scanned"] for i in (first, second)]
    asyncio.run(scheduler.run_once(settings))
    after = [_state(i)["scanned"] for i in (first, second)]
    assert all(a > b for a, b in zip(after, before)), "both advance on every round"


def test_many_accounts_all_get_scanned(db, monkeypatch):
    settings = make_settings(**PAPER_KEYS, max_concurrent_cycles=3)
    client = _add_latency(spot_client(ohlcv=QUIET_OHLCV), 0.03)
    _use(monkeypatch, client)
    ids = [(_paper if i % 2 == 0 else (lambda s, e: new_account(s, email=e)))(settings, f"acct{i}@example.com") for i in range(8)]

    asyncio.run(scheduler.run_once(settings))

    for account_id in ids:
        s = _state(account_id)
        assert s["scanned"] is not None and s["signal"] == "none" and s["error"] is None


def test_concurrency_is_real_but_bounded(db, monkeypatch):
    settings = make_settings(**PAPER_KEYS, max_concurrent_cycles=2)
    live = {"now": 0, "max": 0}
    client = spot_client(ohlcv=QUIET_OHLCV)
    real = client.fetch_ohlcv

    async def tracked(*a, **k):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.05)
        try:
            return await real(*a, **k)
        finally:
            live["now"] -= 1

    client.fetch_ohlcv = tracked
    _use(monkeypatch, client)
    for i in range(6):
        _paper(settings, f"c{i}@example.com")

    asyncio.run(scheduler.run_once(settings))

    assert live["max"] == 2, "cycles overlap (not serialized) but never exceed the configured bound"


# --------------------------------------------------------------------------- no write lock across a suspension


def test_no_write_lock_is_ever_held_across_a_suspension_in_quiet_cycles(probe, monkeypatch):
    settings = make_settings(**PAPER_KEYS)
    notifier = FakeNotifier()
    real_send = notifier.send

    async def send(target, message):
        probe.check("notifier")
        await real_send(target, message)

    notifier.send = send
    client = _add_latency(spot_client(ohlcv=QUIET_OHLCV), 0.01, hooks=[probe.check])
    _use(monkeypatch, client, notifier=notifier)
    _paper(settings, "p@example.com")
    new_account(settings, email="l@example.com")

    asyncio.run(scheduler.run_once(settings))

    assert probe.violations == []


def test_no_write_lock_is_held_across_a_suspension_through_live_entries_and_protection(probe, monkeypatch):
    """Two live accounts open real (fake) positions and place protective stops, concurrently."""
    settings = make_settings(max_notional_multiplier=100.0)
    notifier = FakeNotifier()
    real_send = notifier.send

    async def send(target, message):
        probe.check("notifier")
        await real_send(target, message)

    notifier.send = send
    clients = {e: _add_latency(spot_client(), 0.01, hooks=[probe.check]) for e in ("a@example.com", "b@example.com")}
    _use(monkeypatch, clients["a@example.com"], by_email=clients, notifier=notifier)
    ids = {e: new_account(settings, email=e) for e in clients}

    asyncio.run(scheduler.run_once(settings))

    assert probe.violations == []
    for email, account_id in ids.items():
        trade = the_trade(account_id)
        assert trade.status == "open" and trade.protection_status == "protected", email
        assert len(clients[email].orders_by_role("entry")) == 1, "exactly one entry each"


def test_paper_entry_and_exit_hold_no_write_lock_and_the_daily_summary_sees_the_exit(probe, monkeypatch):
    settings = make_settings(**PAPER_KEYS)
    notifier = FakeNotifier()
    real_send = notifier.send

    async def send(target, message):
        probe.check("notifier")
        await real_send(target, message)

    notifier.send = send
    client = _add_latency(spot_client(ohlcv=QUIET_OHLCV), 0.01, hooks=[probe.check])
    _use(monkeypatch, client, notifier=notifier)
    account_id = _paper(settings, "papertrade@example.com")
    with session_scope() as session:
        session.add(Trade(account_id=account_id, side="long", is_paper=True, entry_price=100.0, quantity=1.0,
                          stop_loss_price=10_000.0, status="open", pair="BTC/USDT"))  # price is below the stop: exits this cycle

    asyncio.run(scheduler.run_once(settings))

    assert probe.violations == []
    assert the_trade(account_id).status == "closed" and the_trade(account_id).close_reason == "stop_loss"
    assert any("Daily P&L summary" in t and "1 trade(s) closed" in t for t in notifier.texts), notifier.texts


# --------------------------------------------------------------------------- failures are never silent


def test_a_session_failure_is_logged_with_the_account_id_recorded_and_isolated(db, monkeypatch, caplog):
    """The original crash: after a failed flush, reading `account.id` in the error handler raised
    PendingRollbackError, so the error vanished. Now it is logged, rolled back and recorded; the other
    account is untouched."""
    settings = make_settings(**PAPER_KEYS)
    client = spot_client(ohlcv=QUIET_OHLCV)
    _use(monkeypatch, client)
    bad, good = _paper(settings, "bad@example.com"), _paper(settings, "good@example.com")
    with session_scope() as session:
        customer_id = session.get(TradingAccount, bad).customer_id

    async def break_the_session(session, account, settings, notifier, real):
        account.last_scanned_at = datetime.now(timezone.utc)
        session.add(Customer(id=customer_id, email="dup@example.com", password_hash="x"))  # duplicate primary key
        session.flush()  # raises IntegrityError and leaves the transaction needing a rollback

    _wrap_process_account(monkeypatch, {bad: break_the_session})
    caplog.set_level(logging.INFO)

    asyncio.run(scheduler.run_once(settings))

    text = caplog.text
    assert f"account {bad}: error during processing cycle" in text and "IntegrityError" in text
    assert "PendingRollbackError" not in text, "the error handler itself must survive a failed session"
    s = _state(bad)
    assert s["scanned"] is not None and s["error"] == scheduler.CYCLE_FAILED_MESSAGE, "evidence is recorded from a fresh session"
    g = _state(good)
    assert g["scanned"] is not None and g["signal"] == "none" and g["error"] is None
    assert "1 failed" in text, "the round summary reports it"


def test_a_database_lock_held_by_someone_else_is_loud_and_the_next_round_recovers(fast_lock_db, monkeypatch, caplog, tmp_path):
    settings = make_settings(**PAPER_KEYS)
    _use(monkeypatch, spot_client(ohlcv=QUIET_OHLCV))
    ids = [_paper(settings, f"lock{i}@example.com") for i in range(2)]
    caplog.set_level(logging.INFO)

    other = sqlite3.connect(f"{tmp_path}/test.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")  # another process holding the write lock
    try:
        asyncio.run(scheduler.run_once(settings))
    finally:
        other.execute("ROLLBACK")
        other.close()

    for account_id in ids:
        assert f"account {account_id}: error during processing cycle" in caplog.text
    assert "database is locked" in caplog.text and "PendingRollbackError" not in caplog.text
    assert "2 failed" in caplog.text

    asyncio.run(scheduler.run_once(settings))  # lock released
    for account_id in ids:
        s = _state(account_id)
        assert s["scanned"] is not None and s["signal"] == "none" and s["error"] is None


def test_task_outcomes_are_inspected_never_discarded_by_the_round(db, monkeypatch, caplog):
    settings = make_settings(**PAPER_KEYS)
    _use(monkeypatch, spot_client(ohlcv=QUIET_OHLCV))
    bad, good = _paper(settings, "x@example.com"), _paper(settings, "y@example.com")
    real = scheduler._run_one

    async def run_one(account_id, settings, slots=None):
        if account_id == bad:
            raise RuntimeError("synthetic escaped failure")
        return await real(account_id, settings, slots)

    monkeypatch.setattr(scheduler, "_run_one", run_one)
    caplog.set_level(logging.INFO)

    asyncio.run(scheduler.run_once(settings))

    assert f"account {bad}: error during processing cycle: RuntimeError: synthetic escaped failure" in caplog.text
    assert _state(good)["scanned"] is not None


def test_the_heartbeat_is_persisted_even_when_the_cycle_then_fails(db, monkeypatch):
    settings = make_settings(**PAPER_KEYS)
    client = spot_client(ohlcv=QUIET_OHLCV)

    async def outage(*a, **k):
        raise ccxt.ExchangeNotAvailable("synthetic outage detail that must never reach the customer")

    client.fetch_ohlcv = outage
    _use(monkeypatch, client)
    account_id = _paper(settings, "hb@example.com")

    asyncio.run(scheduler.run_once(settings))

    s = _state(account_id)
    assert s["scanned"] is not None
    assert s["error"] and "synthetic" not in s["error"], "a safe, generic message - never raw exchange text"


# --------------------------------------------------------------------------- one account never blocks another


def test_a_stuck_account_neither_blocks_others_nor_later_rounds_nor_piles_up(db, monkeypatch, caplog):
    settings = make_settings(**PAPER_KEYS, cycle_watchdog_seconds=1)
    _use(monkeypatch, spot_client(ohlcv=QUIET_OHLCV))
    stuck, healthy = _paper(settings, "stuck@example.com"), _paper(settings, "healthy@example.com")

    async def hang(session, account, settings, notifier, real):
        account.last_scanned_at = datetime.now(timezone.utc)
        session.commit()  # the heartbeat, as the real cycle does
        await asyncio.sleep(3600)

    _wrap_process_account(monkeypatch, {stuck: hang})
    caplog.set_level(logging.INFO)

    async def two_rounds():
        t = time.monotonic()
        await scheduler.run_once(settings)
        first = (time.monotonic() - t, _state(healthy)["scanned"], _state(stuck))
        await scheduler.run_once(settings)
        return first, _state(healthy)["scanned"]

    (elapsed, healthy_after_first, stuck_state), healthy_after_second = asyncio.run(two_rounds())

    assert elapsed < 2.5, "the round is not held up by the straggler"
    assert healthy_after_first is not None and healthy_after_second > healthy_after_first, "the healthy account keeps being scanned"
    assert stuck_state["scanned"] is not None, "the stuck cycle's heartbeat was already persisted"
    assert f"account {stuck}: processing cycle still running after" in caplog.text
    assert f"account {stuck}: its previous cycle is still running, skipping this round" in caplog.text


def test_a_stuck_reconciliation_does_not_stop_another_accounts_position_being_managed(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0, cycle_watchdog_seconds=1)
    clients = {e: spot_client() for e in ("a@example.com", "b@example.com")}
    _use(monkeypatch, clients["a@example.com"], by_email=clients)
    ids = {e: new_account(settings, email=e) for e in clients}
    asyncio.run(scheduler.run_once(settings))  # both open protected positions
    for client in clients.values():  # the customers remove their stops on Binance
        stop = [o for o in client.orders_by_role("stop") if o["status"] == "open"][0]
        stop["status"] = "canceled"
        client._unlock_base(stop["amount"] - stop["filled"])

    real = scheduler.reconcile_account

    async def reconcile(session, account, settings, notifier, **kw):
        if account.id == ids["a@example.com"]:
            await asyncio.sleep(3600)
        return await real(session, account, settings, notifier, **kw)

    monkeypatch.setattr(scheduler, "reconcile_account", reconcile)
    asyncio.run(scheduler.recover_once(settings, include_healthy=True))

    b = the_trade(ids["b@example.com"])
    live_stops = [o for o in clients["b@example.com"].orders_by_role("stop") if o["status"] == "open"]
    assert b.protection_status == "protected" and len(live_stops) == 1, "B's protection was restored despite A hanging"


def test_poll_and_recovery_loops_run_concurrently_across_accounts_without_contention(probe, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    clients = {e: _add_latency(spot_client(), 0.01, hooks=[probe.check]) for e in ("a@example.com", "b@example.com")}
    _use(monkeypatch, clients["a@example.com"], by_email=clients)
    ids = {e: new_account(settings, email=e) for e in clients}
    asyncio.run(scheduler.run_once(settings))
    for client in clients.values():
        stop = [o for o in client.orders_by_role("stop") if o["status"] == "open"][0]
        stop["status"] = "canceled"
        client._unlock_base(stop["amount"] - stop["filled"])
    probe.violations.clear()

    async def both():
        await asyncio.gather(scheduler.run_once(settings), scheduler.recover_once(settings, include_healthy=True))

    asyncio.run(both())

    assert probe.violations == []
    for email, account_id in ids.items():
        live_stops = [o for o in clients[email].orders_by_role("stop") if o["status"] == "open"]
        assert the_trade(account_id).protection_status == "protected" and len(live_stops) == 1, email
        assert len(clients[email].orders_by_role("entry")) == 1, "no duplicate entry"


# --------------------------------------------------------------------------- supervision and visibility


def test_a_crashed_scheduler_loop_is_logged_and_restarted(monkeypatch, caplog):
    calls = []

    async def make():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("synthetic loop crash")
        raise asyncio.CancelledError

    async def no_sleep(_):
        return None

    monkeypatch.setattr(scheduler.asyncio, "sleep", no_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler._supervised("poll loop", make))

    assert len(calls) == 2
    assert "the scheduler's poll loop stopped unexpectedly" in caplog.text and "synthetic loop crash" in caplog.text


def test_a_scheduler_task_that_ends_is_reported_loudly(caplog):
    async def go():
        task = asyncio.ensure_future(_boom())
        await asyncio.wait([task])
        app_module._log_scheduler_task_ended(task)

    async def _boom():
        raise RuntimeError("synthetic scheduler death")

    asyncio.run(go())
    assert "the scheduler task ended unexpectedly: RuntimeError" in caplog.text


def test_scheduler_logs_become_visible_under_uvicorn_but_never_override_existing_logging(monkeypatch):
    package = logging.getLogger("mba_bot")
    root = logging.getLogger()
    saved = (list(package.handlers), package.level, list(root.handlers))
    try:
        # Already configured (pytest, main.py's basicConfig): untouched.
        app_module._ensure_scheduler_logs_are_visible()
        assert package.handlers == saved[0]

        # Bare uvicorn: no handlers anywhere -> the package gets an INFO stream handler.
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in list(package.handlers):
            package.removeHandler(h)
        app_module._ensure_scheduler_logs_are_visible()
        assert len(package.handlers) == 1 and package.level == logging.INFO
        app_module._ensure_scheduler_logs_are_visible()
        assert len(package.handlers) == 1, "idempotent"
    finally:
        for h in list(package.handlers):
            package.removeHandler(h)
        for h in saved[0]:
            package.addHandler(h)
        package.setLevel(saved[1])
        for h in saved[2]:
            root.addHandler(h)
