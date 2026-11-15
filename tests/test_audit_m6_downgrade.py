"""Audit M6: downgrading the chain below c9f6a3b1d5e7 must not silently end strategy pauses. It REFUSES, changing nothing, while an active pause exists."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parent.parent
U = timezone.utc


def add_pause(path, *, resume, released=None, code="strategy_expectancy_pause", pid="p1"):
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(text("insert into strategy_pauses (id,account_id,strategy_id,code,is_paper,paused_at,resume_at,released_at,detail) values (:i,'a1','ema_cross_atr_v1',:c,0,:p,:r,:x,'')"),
                  dict(i=pid, c=code, p=datetime.now(U).isoformat(), r=None if resume is None else resume.isoformat(), x=None if released is None else released.isoformat()))


@pytest.fixture
def cfg(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    c = Config(str(ROOT / "alembic.ini"))
    command.upgrade(c, "c9f6a3b1d5e7")
    return c, path


def has_table(path):
    return inspect(create_engine(f"sqlite:///{path}")).has_table("strategy_pauses")


@pytest.mark.parametrize("resume", [None, datetime.now(U) + timedelta(hours=5)], ids=["until released", "still running"])
def test_the_downgrade_refuses_while_a_pause_is_active_and_changes_nothing(cfg, resume):
    c, path = cfg
    add_pause(path, resume=resume)
    with pytest.raises(Exception, match="active strategy pause"):
        command.downgrade(c, "b8e5f2a0c4d6")
    assert has_table(path)
    with create_engine(f"sqlite:///{path}").connect() as conn:
        assert conn.execute(text("select count(*) from strategy_pauses")).scalar() == 1


def test_released_expired_or_no_pauses_do_not_block_the_downgrade(cfg):
    c, path = cfg
    add_pause(path, resume=None, released=datetime.now(U) - timedelta(hours=1), pid="p1")  # released by the owner
    add_pause(path, resume=datetime.now(U) - timedelta(hours=1), pid="p2", code="strategy_loss_streak_pause")  # ran out
    command.downgrade(c, "b8e5f2a0c4d6")
    assert not has_table(path)
    command.upgrade(c, "head")
    assert has_table(path)


def test_an_empty_table_downgrades_and_a_missing_table_is_not_an_error(cfg):
    c, path = cfg
    command.downgrade(c, "b8e5f2a0c4d6")
    assert not has_table(path)


def test_the_rollback_sections_say_what_is_lost():
    guards = (ROOT / "docs/safety/strategy_guards.md").read_text()
    multi = (ROOT / "docs/safety/multi_trade.md").read_text()
    assert "strategy_pauses" in guards and "refuses" in guards and "active pause" in guards
    assert "strategy_id" in multi and "relabel" in multi and "pair_list" in multi and "extra_strategies" in multi
