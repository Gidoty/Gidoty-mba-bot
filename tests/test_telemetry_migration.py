"""The telemetry migration must be safe in any order relative to the service's own create_all(), and must leave one head."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from mba_bot.models import Base

ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location("telemetry_migration", next((ROOT / "alembic" / "versions").glob("d4a8f1c3e6b2_*.py")))
MIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIG)
TABLES = ("trade_telemetry", "decision_log")
INDEXES = {"ix_decision_log_ts", "ix_decision_log_account_ts", "ix_decision_log_reason"}


def run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def state(engine):
    insp = inspect(engine)
    return [insp.has_table(t) for t in TABLES], {i["name"] for i in insp.get_indexes("decision_log")} if insp.has_table("decision_log") else set()


def test_upgrade_on_a_database_that_lacks_the_tables_creates_them_and_repeats_safely(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/a.db")
    run(eng, MIG.upgrade)
    assert state(eng) == ([True, True], INDEXES)
    run(eng, MIG.upgrade)
    assert state(eng) == ([True, True], INDEXES)


def test_upgrade_after_the_service_already_created_the_tables_does_nothing_and_does_not_fail(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/b.db")
    Base.metadata.create_all(eng)  # what init_db() does at every service start
    before = state(eng)
    assert before[0] == [True, True]
    run(eng, MIG.upgrade)
    assert state(eng) == before


def test_downgrade_removes_them_and_is_repeatable(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/c.db")
    run(eng, MIG.upgrade)
    run(eng, MIG.downgrade)
    assert state(eng) == ([False, False], set())
    run(eng, MIG.downgrade)


def test_the_migration_chain_has_exactly_one_head_and_it_follows_the_production_head():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["d4a8f1c3e6b2"]
    assert MIG.down_revision == "c7d2e5f8a1b3"
