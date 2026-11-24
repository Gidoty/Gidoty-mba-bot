"""Audit B1: the database again allows only ONE non-terminal LIVE trade per account (alembic e7a2c9d4b1f8, models.Trade), whatever the pair.
Paper rows are outside the rule, so paper multi-trade keeps working. (Since B7 the same rule is kept per slot, d9b5f7c2e4a6: with every live trade in slot 0 it is unchanged, and the first tests below
run against the current schema; the migration tests stop at e7a2c9d4b1f8, the revision they are about.)"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from helpers_live import make_settings, new_account
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade

ROOT = Path(__file__).resolve().parent.parent
HEAD_BEFORE = "a1d7c4e9b2f6"
NEW = "e7a2c9d4b1f8"
INDEX = "uq_trades_one_live_nonterminal_per_account"


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def row(account_id, status, pair, *, paper):
    return Trade(account_id=account_id, side="long", is_paper=paper, entry_price=1.0, quantity=1.0, stop_loss_price=0.5, status=status, pair=pair)


def test_a_second_live_trade_on_a_different_pair_is_rejected_by_the_database(db):
    aid = new_account(make_settings())
    with session_scope() as s:
        s.add(row(aid, "open", "BTC/USDT", paper=False))
    for status in ("pending", "protecting", "open", "closing", "attention"):
        with pytest.raises(IntegrityError):
            with session_scope() as s:
                s.add(row(aid, status, "ETH/USDT", paper=False))


def test_paper_trades_on_different_pairs_and_a_paper_trade_beside_a_live_one_are_allowed(db):
    aid = new_account(make_settings())
    with session_scope() as s:
        s.add(row(aid, "open", "BTC/USDT", paper=True))
        s.add(row(aid, "open", "ETH/USDT", paper=True))
        s.add(row(aid, "open", "BNB/USDT", paper=True))
        s.add(row(aid, "open", "XRP/USDT", paper=False))  # one live trade next to the paper ones
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(row(aid, "pending", "BNB/USDT", paper=False))


def test_terminal_live_trades_and_other_accounts_do_not_count(db):
    a = new_account(make_settings(), email="a@example.com")
    b = new_account(make_settings(), email="b@example.com")
    with session_scope() as s:
        for status in ("closed", "void", "closed"):
            s.add(row(a, status, "BTC/USDT", paper=False))
        s.add(row(a, "open", "ETH/USDT", paper=False))
        s.add(row(b, "open", "ETH/USDT", paper=False))


def _cfg(monkeypatch, path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    return Config(str(ROOT / "alembic.ini"))


def _insert(path, rows):
    eng = create_engine(f"sqlite:///{path}")
    now = datetime.now(timezone.utc).isoformat()
    with eng.begin() as c:
        c.execute(text("insert or ignore into customers (id,email,password_hash,created_at,is_active,has_paid) values ('c1','m@example.com','x',:n,1,1)"), dict(n=now))
        c.execute(text("insert or ignore into trading_accounts (id,customer_id,pair,market_type,pair_selection_mode,notification_channel,risk_pct,trial_ends_at,paper_balance,is_active,created_at,updated_at) "
                       "values ('a1','c1','BTC/USDT','spot','manual','telegram',1.0,:n,10000,1,:n,:n)"), dict(n=now))
        for i, (pair, paper, status) in enumerate(rows):
            c.execute(text("insert into trades (id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,status,opened_at,pair) values (:i,'a1','long',:p,100,1,90,:s,:n,:pair)"),
                      dict(i=f"t{i}", p=int(paper), s=status, n=now, pair=pair))


def test_the_migration_is_additive_idempotent_and_reversible(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    cfg = _cfg(monkeypatch, path)
    command.upgrade(cfg, HEAD_BEFORE)
    names = lambda: {i["name"] for i in inspect(create_engine(f"sqlite:///{path}")).get_indexes("trades")}
    assert INDEX not in names()
    _insert(path, [("BTC/USDT", False, "open"), ("ETH/USDT", True, "open"), ("BNB/USDT", True, "open")])  # one live, two paper: allowed
    command.upgrade(cfg, NEW)
    assert INDEX in names()
    command.upgrade(cfg, NEW)  # a rerun is a no-op
    command.downgrade(cfg, HEAD_BEFORE)
    assert INDEX not in names()
    eng = create_engine(f"sqlite:///{path}")
    with eng.connect() as c:
        assert c.execute(text("select count(*) from trades")).scalar() == 3  # no row touched
    command.upgrade(cfg, NEW)
    assert INDEX in names()


def test_the_upgrade_refuses_when_an_account_already_has_two_live_trades_and_changes_nothing(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "d.db"
    cfg = _cfg(monkeypatch, path)
    command.upgrade(cfg, HEAD_BEFORE)
    _insert(path, [("BTC/USDT", False, "open"), ("ETH/USDT", False, "pending")])
    with pytest.raises(Exception, match="more than one non-terminal LIVE trade"):
        command.upgrade(cfg, NEW)
    assert INDEX not in {i["name"] for i in inspect(create_engine(f"sqlite:///{path}")).get_indexes("trades")}


def test_the_chain_has_one_head_on_top_of_the_multi_trade_migration():
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_revision(NEW).down_revision == HEAD_BEFORE and script.get_heads() == ["e5b2c8d4f1a7"]  # (the head moved on with the later migrations)


def test_the_upgrade_does_not_fail_when_the_index_already_exists(tmp_path, monkeypatch, keep_logging):
    """init_db() runs create_all(), which creates the new index on a database the migration has not reached yet; the migration must then be a no-op."""
    path = tmp_path / "pre.db"
    cfg = _cfg(monkeypatch, path)
    command.upgrade(cfg, HEAD_BEFORE)
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(text(f"CREATE UNIQUE INDEX {INDEX} ON trades (account_id) WHERE status NOT IN ('closed', 'void') AND NOT is_paper"))
    command.upgrade(cfg, NEW)
    assert INDEX in {i["name"] for i in inspect(create_engine(f"sqlite:///{path}")).get_indexes("trades")}
