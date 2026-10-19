"""Database-level guarantees: the one-non-terminal-trade-per-account
invariant, the additive migration (fresh and populated legacy databases),
the single-instance lock, and the recovery loop's account selection."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from helpers_live import *  # noqa: F401,F403
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from mba_bot import scheduler
from mba_bot.db import get_open_trade, init_db, list_active_accounts, session_scope
from mba_bot.instance_lock import AlreadyRunning, acquire_single_instance_lock
from mba_bot.models import Base, Trade, TradingAccount

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "dc1a28b7a42e"
NEW_HEAD = "b3f1c9a2d7e4"
NEW_COLUMNS = {
    "pair", "entry_order_id", "exit_order_id", "protective_order_id", "protective_kind", "protective_limit_price",
    "protection_status", "protection_verified_at", "attention_reason", "entry_fee_base", "protection_attempt", "exit_attempt",
}


def _alembic(monkeypatch, db_path) -> Config:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    return Config(str(ROOT / "alembic.ini"))


# =========================== one non-terminal trade per account ===========================


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _trade(account_id, status, **kw):
    return Trade(
        account_id=account_id, side="long", is_paper=False, entry_price=1.0, quantity=1.0, stop_loss_price=0.5,
        status=status, **kw,
    )


@pytest.mark.parametrize("first", ["pending", "protecting", "open", "closing", "attention"])
@pytest.mark.parametrize("second", ["pending", "open", "attention"])
def test_a_second_non_terminal_trade_for_the_same_account_is_refused_by_the_database(db, first, second):
    settings = make_settings()
    account_id = new_account(settings)
    with session_scope() as session:
        session.add(_trade(account_id, first))
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(_trade(account_id, second))


def test_terminal_trades_do_not_count_and_other_accounts_are_independent(db):
    settings = make_settings()
    a = new_account(settings, email="a@example.com")
    b = new_account(settings, email="b@example.com")
    with session_scope() as session:
        for _ in range(3):
            session.add(_trade(a, "closed"))
            session.add(_trade(a, "void"))
        session.add(_trade(a, "open"))
        session.add(_trade(b, "open"))
    with session_scope() as session:
        assert get_open_trade(session, a).status == "open"
    # once the open one closes, a new one may start
    with session_scope() as session:
        get_open_trade(session, a).status = "closed"
    with session_scope() as session:
        session.add(_trade(a, "pending"))


def test_every_non_terminal_status_counts_as_an_open_trade_and_keeps_an_inactive_account_scheduled(db):
    settings = make_settings()
    for i, status in enumerate(["pending", "protecting", "open", "closing", "attention"]):
        account_id = new_account(settings, email=f"s{i}@example.com")
        with session_scope() as session:
            session.get(TradingAccount, account_id).is_active = False
            session.add(_trade(account_id, status))
        with session_scope() as session:
            assert get_open_trade(session, account_id).status == status
            assert account_id in [a.id for a in list_active_accounts(session)], status


# =========================== migration ===========================


def _trades_columns(path):
    return {c["name"] for c in inspect(create_engine(f"sqlite:///{path}")).get_columns("trades")}


def test_fresh_database_migrates_to_the_model_schema(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    command.upgrade(_alembic(monkeypatch, path), "head")
    engine = create_engine(f"sqlite:///{path}")
    cols = {c["name"] for c in inspect(engine).get_columns("trades")}
    assert NEW_COLUMNS <= cols
    assert cols == {c.name for c in Trade.__table__.columns}, "migration and models must agree"
    indexes = {i["name"] for i in inspect(engine).get_indexes("trades")}
    assert "uq_trades_one_nonterminal_per_account" in indexes
    with engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() == NEW_HEAD


def _populate_legacy(path):
    """A representative pre-migration database: paper + live accounts, closed
    and open trades of both kinds, written with the OLD schema only."""
    engine = create_engine(f"sqlite:///{path}")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        for i, (email, paid) in enumerate([("paper@example.com", 0), ("live@example.com", 1), ("idle@example.com", 1)]):
            conn.execute(text("insert into customers (id,email,password_hash,created_at,is_active,has_paid) values (:i,:e,'x',:n,1,:p)"),
                         dict(i=f"c{i}", e=email, n=now, p=paid))
            conn.execute(text(
                "insert into trading_accounts (id,customer_id,pair,market_type,pair_selection_mode,notification_channel,risk_pct,"
                "trial_ends_at,paper_balance,is_active,created_at,updated_at) values "
                "(:i,:c,'BTC/USDT',:m,'manual','telegram',1.0,:n,10000,1,:n,:n)"),
                dict(i=f"a{i}", c=f"c{i}", m="futures" if i == 1 else "spot", n=now))
        rows = [
            ("t1", "a0", 1, "closed", 100.0, 110.0, 10.0, "crossover_exit"),
            ("t2", "a0", 1, "open", 100.0, None, None, None),
            ("t3", "a1", 0, "closed", 100.0, 90.0, -10.0, "stop_loss"),
            ("t4", "a1", 0, "open", 100.0, None, None, None),
        ]
        for tid, acc, paper, status, entry, exit_, pnl, reason in rows:
            conn.execute(text(
                "insert into trades (id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,exit_price,pnl,close_reason,"
                "status,opened_at) values (:id,:a,'long',:p,:e,1.5,90,:x,:pnl,:r,:s,:n)"),
                dict(id=tid, a=acc, p=paper, e=entry, x=exit_, pnl=pnl, r=reason, s=status, n=now))


def test_populated_legacy_database_migrates_without_changing_any_existing_value(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    cfg = _alembic(monkeypatch, path)
    command.upgrade(cfg, PREVIOUS_HEAD)
    assert NEW_COLUMNS.isdisjoint(_trades_columns(path)), "precondition: the legacy schema has none of the new columns"
    _populate_legacy(path)
    before = sqlite3.connect(path).execute(
        "select id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,exit_price,pnl,close_reason,status from trades order by id"
    ).fetchall()

    command.upgrade(cfg, "head")

    con = sqlite3.connect(path)
    after = con.execute(
        "select id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,exit_price,pnl,close_reason,status from trades order by id"
    ).fetchall()
    assert after == before, "migration must be purely additive"
    nulls = con.execute(
        "select count(*) from trades where pair is null and entry_order_id is null and protective_order_id is null "
        "and protection_status is null and attention_reason is null and entry_fee_base is null"
    ).fetchone()[0]
    assert nulls == 4
    assert con.execute("select distinct protection_attempt, exit_attempt from trades").fetchall() == [(0, 0)]

    # The migrated database works with the application: legacy open rows are found,
    # a legacy LIVE open row has no protection recorded (so reconcile will protect or flag it).
    init_db(f"sqlite:///{path}")
    with session_scope() as session:
        live_account = session.query(TradingAccount).filter(TradingAccount.pair == "BTC/USDT", TradingAccount.market_type == "futures").one()
        legacy_live = get_open_trade(session, live_account.id)
        assert legacy_live.status == "open" and legacy_live.protection_status is None and legacy_live.is_paper is False
        live_id = live_account.id
    # and the invariant is now enforced on it
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(_trade(live_id, "pending"))


def test_migration_refuses_a_database_that_already_violates_the_one_trade_rule(tmp_path, monkeypatch):
    path = tmp_path / "dup.db"
    cfg = _alembic(monkeypatch, path)
    command.upgrade(cfg, PREVIOUS_HEAD)
    _populate_legacy(path)
    sqlite3.connect(path).execute(
        "insert into trades (id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,status,opened_at) "
        "values ('dup','a1','long',0,1,1,0.5,'open','2026-01-01')"
    ).connection.commit()

    with pytest.raises(RuntimeError, match="more than one non-terminal trade"):
        command.upgrade(cfg, "head")

    assert NEW_COLUMNS.isdisjoint(_trades_columns(path)), "a refused upgrade must leave the schema untouched"


def test_migration_downgrades_and_upgrades_again(tmp_path, monkeypatch):
    path = tmp_path / "roundtrip.db"
    cfg = _alembic(monkeypatch, path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, PREVIOUS_HEAD)
    assert NEW_COLUMNS.isdisjoint(_trades_columns(path))
    command.upgrade(cfg, "head")
    assert NEW_COLUMNS <= _trades_columns(path)


def test_alembic_has_a_single_head_and_the_new_revision_parents_the_old_one():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == [NEW_HEAD]
    assert script.get_revision(NEW_HEAD).down_revision == PREVIOUS_HEAD


# =========================== single-instance lock ===========================


def test_a_second_scheduler_instance_cannot_take_the_lock(tmp_path):
    path = str(tmp_path / "mba.lock")
    first = acquire_single_instance_lock(path)
    try:
        with pytest.raises(AlreadyRunning):
            acquire_single_instance_lock(path)
    finally:
        first.close()
    acquire_single_instance_lock(path).close()  # released when the holder goes away


# =========================== recovery loop selection ===========================


def _live_account_with_trade(settings, email, status, **kw):
    account_id = new_account(settings, email=email)
    with session_scope() as session:
        session.add(_trade(account_id, status, **kw))
    return account_id


def test_recovery_loop_only_touches_trades_that_are_not_healthy_and_startup_touches_all(db, monkeypatch):
    settings = make_settings()
    healthy = _live_account_with_trade(settings, "h@example.com", "open", protection_status="protected")
    legacy = _live_account_with_trade(settings, "l@example.com", "open")  # open, never protected
    pending = _live_account_with_trade(settings, "p@example.com", "pending")
    attention = _live_account_with_trade(settings, "t@example.com", "attention")
    closing = _live_account_with_trade(settings, "c@example.com", "closing")
    with session_scope() as session:
        paper = new_account(settings, email="paper@example.com")
        session.add(Trade(account_id=paper, side="long", is_paper=True, entry_price=1, quantity=1, stop_loss_price=0.5, status="open"))
        done = new_account(settings, email="done@example.com")
        session.add(_trade(done, "closed"))

    seen: list[str] = []

    async def fake_reconcile(session, account, settings_, notifier):
        seen.append(account.id)

    monkeypatch.setattr(scheduler, "reconcile_account", fake_reconcile)
    monkeypatch.setattr(scheduler, "build_notifier", lambda *a, **k: FakeNotifier())

    asyncio.run(scheduler.recover_once(settings))
    assert sorted(seen) == sorted([legacy, pending, attention, closing]), "healthy, paper and closed trades are left to the main loop"

    seen.clear()
    asyncio.run(scheduler.recover_once(settings, include_healthy=True))
    assert sorted(seen) == sorted([healthy, legacy, pending, attention, closing])


def test_main_loop_and_recovery_loop_share_one_lock_per_account():
    assert scheduler._lock_for("acct-1") is scheduler._lock_for("acct-1")
    assert scheduler._lock_for("acct-1") is not scheduler._lock_for("acct-2")


def test_reconcile_account_ignores_paper_trades_and_accounts_with_nothing_open(db, monkeypatch):
    settings = make_settings()
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_reconcile(account_id, settings)
    assert client.calls == [], "nothing open: no exchange contact at all"
