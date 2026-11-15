"""Audit H1: `python -m mba_bot.restart_gate` must work in a FRESH process (no init_db, no create_all), exactly as the owner runs it, and must never write."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent


def gate(db_path, *, extra_env=None):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src"), "DATABASE_URL": f"sqlite:///{db_path}"}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "mba_bot.restart_gate"], capture_output=True, text=True, env=env, cwd=str(ROOT.parent))


@pytest.fixture
def migrated(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "prod.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    return path


def add_trade(path, status, *, paper, pair="BTC/USDT", tid="t1"):
    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(path)
    con.execute("insert or ignore into customers (id,email,password_hash,created_at,is_active,has_paid) values ('c1','m@example.com','x',?,1,1)", (now,))
    con.execute("insert or ignore into trading_accounts (id,customer_id,pair,market_type,pair_selection_mode,notification_channel,risk_pct,trial_ends_at,paper_balance,is_active,created_at,updated_at) "
                "values ('a1','c1','BTC/USDT','spot','manual','telegram',1.0,?,10000,1,?,?)", (now, now, now))
    con.execute("insert into trades (id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,status,opened_at,pair) values (?,'a1','long',?,100,1,90,?,?,?)",
                (tid, int(paper), status, now, pair))
    con.commit()
    con.close()


def test_a_fresh_process_says_clear_on_a_migrated_database_with_no_live_trade(migrated):
    r = gate(migrated)
    assert r.returncode == 0 and "no live trade open" in r.stdout, (r.stdout, r.stderr)


def test_a_fresh_process_says_do_not_restart_with_one_live_trade_and_prints_counts_only(migrated):
    add_trade(migrated, "open", paper=False)
    r = gate(migrated)
    assert r.returncode == 3 and "1 live trade(s) open" in r.stdout and "do NOT restart" in r.stdout, (r.stdout, r.stderr)
    assert "a1" not in r.stdout and "BTC" not in r.stdout and "m@example.com" not in r.stdout + r.stderr


def test_paper_and_terminal_trades_do_not_block_a_restart(migrated):
    add_trade(migrated, "open", paper=True, tid="t1")
    add_trade(migrated, "closed", paper=False, tid="t2")
    add_trade(migrated, "void", paper=False, tid="t3")
    assert gate(migrated).returncode == 0


def test_a_missing_database_file_or_a_missing_table_is_exit_2_and_creates_nothing(tmp_path):
    missing = tmp_path / "nothing.db"
    r = gate(missing)
    assert r.returncode == 2 and "do NOT restart" in r.stderr and not missing.exists()
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    r = gate(empty)
    assert r.returncode == 2
    con = sqlite3.connect(empty)
    assert con.execute("select count(*) from sqlite_master").fetchone()[0] == 0  # no table created by the gate
    con.close()


def test_the_gate_never_writes_the_database(migrated):
    add_trade(migrated, "open", paper=False)
    before = migrated.read_bytes()
    gate(migrated)
    assert migrated.read_bytes() == before


def test_the_gate_does_not_depend_on_init_db_or_create_all():
    src = (ROOT / "src/mba_bot/restart_gate.py").read_text()
    assert "init_db" not in src.replace("no init_db", "") and "create_all" not in src.replace("no create_all", "") and "session_scope" not in src


def test_the_count_is_the_number_of_live_trades_across_accounts(migrated):
    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(migrated)
    for i in (1, 2, 3):
        con.execute("insert into customers (id,email,password_hash,created_at,is_active,has_paid) values (?,?,'x',?,1,1)", (f"c{i}", f"m{i}@example.com", now))
        con.execute("insert into trading_accounts (id,customer_id,pair,market_type,pair_selection_mode,notification_channel,risk_pct,trial_ends_at,paper_balance,is_active,created_at,updated_at) "
                    "values (?,?,'BTC/USDT','spot','manual','telegram',1.0,?,10000,1,?,?)", (f"a{i}", f"c{i}", now, now, now))
        con.execute("insert into trades (id,account_id,side,is_paper,entry_price,quantity,stop_loss_price,status,opened_at,pair) values (?,?,'long',0,100,1,90,'open',?,'BTC/USDT')", (f"t{i}", f"a{i}", now))
    con.commit()
    con.close()
    r = gate(migrated)
    assert r.returncode == 3 and "3 live trade(s) open (open=3)" in r.stdout, r.stdout
