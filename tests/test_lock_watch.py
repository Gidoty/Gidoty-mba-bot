"""Lock diagnosis (docs/safety/lock_watch.md): observation only.

PROVES: a write transaction held longer than the threshold gives ONE warning line with the duration, how it ended and function names only (no statement text, no values, no ids); a short one, a read-only one and a
non-SQLite engine give nothing; at most 20 lines an hour; a clock, a call-site walk, a log call or a counter that fails can never raise into the database layer; a rollback and a connection returned with its write
open are timed too; the start-up probe writes one line with the wall time and the write holds, and passes the step's own exception through; the setting is on at 3 seconds by default and 0 switches it off.
DOES NOT PROVE: which writer held the production lock. It only produces the evidence."""
from __future__ import annotations

import logging
import time

import pytest
from sqlalchemy import create_engine, text

from mba_bot import config, lock_watch


@pytest.fixture(autouse=True)
def fresh():
    lock_watch.reset_for_tests()
    yield
    lock_watch.reset_for_tests()


def make_engine(tmp_path, *, threshold=0.05, max_lines=20, install=True):
    eng = create_engine(f"sqlite:///{tmp_path}/w.db", connect_args={"check_same_thread": False})
    with eng.begin() as c:
        c.execute(text("create table t (id integer primary key, v text)"))
    if install:
        assert lock_watch.install(eng, threshold_seconds=threshold, max_lines_per_hour=max_lines) is True
    return eng


def slow_writer(eng, secret="SECRET-VALUE-123", pause=0.12):
    with eng.begin() as c:
        c.execute(text("insert into t (v) values (:v)"), {"v": secret})
        time.sleep(pause)


def warnings(caplog):
    return [r for r in caplog.records if r.name == "mba_bot.lock_watch" and r.levelno == logging.WARNING]


def test_a_long_write_transaction_gives_one_line_with_duration_and_function_names_only(tmp_path, caplog):
    eng = make_engine(tmp_path)
    with caplog.at_level("WARNING"):
        slow_writer(eng)
    lines = warnings(caplog)
    assert len(lines) == 1
    msg = lines[0].getMessage()
    assert "held the database for 0.1" in msg and "ended by commit" in msg and "started in unknown" in msg  # the test module is not mba_bot code, so no function is named
    assert "SECRET-VALUE-123" not in msg and "insert" not in msg.lower() and "select" not in msg.lower() and "/" not in msg and "\\" not in msg


def test_the_call_site_names_this_package_functions_only_innermost_first_and_at_most_six():
    ns = {"probe": lambda: lock_watch._call_site()}
    code = compile("def f1(): return probe()\ndef f2(): return f1()\ndef f3(): return f2()\ndef f4(): return f3()\ndef f5(): return f4()\ndef f6(): return f5()\ndef f7(): return f6()\n", "/srv/app/mba_bot/fake.py", "exec")
    exec(code, ns)  # noqa: S102
    assert ns["f3"]() == "f1<-f2<-f3"
    assert ns["f7"]().count("<-") == 5  # six names at most
    assert "/" not in ns["f7"]() and "fake" not in ns["f7"]()  # no file path, only function names


def test_short_and_read_only_transactions_give_nothing(tmp_path, caplog):
    eng = make_engine(tmp_path)
    with caplog.at_level("WARNING"):
        with eng.begin() as c:
            c.execute(text("insert into t (v) values ('a')"))  # fast
        with eng.begin() as c:
            c.execute(text("select * from t"))
            time.sleep(0.12)  # long but only reads
    assert warnings(caplog) == []


def test_a_rollback_is_timed_too(tmp_path, caplog):
    eng = make_engine(tmp_path)
    with caplog.at_level("WARNING"):
        with eng.connect() as c:
            c.execute(text("insert into t (v) values ('a')"))
            time.sleep(0.12)
            c.rollback()
    assert len(warnings(caplog)) == 1 and "ended by rollback" in warnings(caplog)[0].getMessage()


def test_a_connection_closed_with_its_write_still_open_is_timed(tmp_path, caplog):
    eng = make_engine(tmp_path)
    with caplog.at_level("WARNING"):
        c = eng.connect()
        c.execute(text("insert into t (v) values ('a')"))
        time.sleep(0.12)
        c.close()  # closes without a commit
    lines = warnings(caplog)
    assert len(lines) == 1 and ("ended by rollback" in lines[0].getMessage() or "ended by close" in lines[0].getMessage())


def test_at_most_the_set_number_of_lines_per_hour(tmp_path, caplog):
    eng = make_engine(tmp_path, threshold=0.001, max_lines=20)
    with caplog.at_level("WARNING"):
        for _ in range(25):
            slow_writer(eng, pause=0.01)
    assert len(warnings(caplog)) == 20


def test_the_duration_covers_the_whole_span_from_the_first_write_not_the_last(tmp_path, caplog, monkeypatch):
    """A transaction with several write statements is timed from the FIRST one. On a fake clock: first write at t=0, a second at t=2.5, a third at t=4.0, commit at t=5.0. The logged hold is 5.0 s, not the 1.0 s since the last write."""
    eng = make_engine(tmp_path, threshold=3.0)
    clock = [100.0]
    monkeypatch.setattr(lock_watch.time, "monotonic", lambda: clock[0])
    with caplog.at_level("WARNING"):
        with eng.begin() as c:
            c.execute(text("insert into t (v) values ('a')"))  # first write: the clock starts here
            clock[0] += 2.5
            c.execute(text("insert into t (v) values ('b')"))  # a later write must not restart it
            clock[0] += 1.5
            c.execute(text("update t set v = 'c' where id = 1"))
            clock[0] += 1.0
    lines = warnings(caplog)
    assert len(lines) == 1
    assert "held the database for 5.0s" in lines[0].getMessage()


def test_a_read_between_writes_does_not_start_or_restart_the_clock(tmp_path, caplog, monkeypatch):
    eng = make_engine(tmp_path, threshold=3.0)
    clock = [100.0]
    monkeypatch.setattr(lock_watch.time, "monotonic", lambda: clock[0])
    with caplog.at_level("WARNING"):
        with eng.begin() as c:
            c.execute(text("select * from t"))
            clock[0] += 10.0  # reads before the first write do not count
            c.execute(text("insert into t (v) values ('a')"))
            clock[0] += 2.0
            c.execute(text("select * from t"))
            clock[0] += 2.0
    assert "held the database for 4.0s" in warnings(caplog)[0].getMessage()


def test_the_hourly_window_slides(tmp_path, caplog, monkeypatch):
    eng = make_engine(tmp_path, threshold=0.001, max_lines=2)
    clock = [1000.0]
    real = time.monotonic
    monkeypatch.setattr(lock_watch.time, "monotonic", lambda: clock[0] + (real() - real()))

    def slow():
        with eng.begin() as c:
            c.execute(text("insert into t (v) values ('a')"))
            clock[0] += 0.5  # the "write" lasted half a second on the fake clock
    with caplog.at_level("WARNING"):
        for _ in range(4):
            slow()
        assert len(warnings(caplog)) == 2
        clock[0] += 3601
        slow()
    assert len(warnings(caplog)) == 3


def test_nothing_is_attached_for_zero_threshold_or_another_database(tmp_path):
    eng = make_engine(tmp_path, install=False)
    assert lock_watch.install(eng, threshold_seconds=0) is False
    from types import SimpleNamespace

    other = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    assert lock_watch.install(other, threshold_seconds=3.0) is False


@pytest.mark.parametrize("what", ["monotonic", "call_site", "log", "stats", "allowed"])
def test_a_failing_hook_never_raises_into_the_database_layer(tmp_path, monkeypatch, what):
    eng = make_engine(tmp_path, threshold=0.0001)

    def boom(*a, **k):
        raise RuntimeError("diagnostic broke")

    if what == "monotonic":
        monkeypatch.setattr(lock_watch.time, "monotonic", boom)
    elif what == "call_site":
        monkeypatch.setattr(lock_watch, "_call_site", boom)
    elif what == "log":
        monkeypatch.setattr(lock_watch.logger, "warning", boom)
    elif what == "stats":
        monkeypatch.setattr(lock_watch, "_stats", None)
    else:
        monkeypatch.setattr(lock_watch, "_allowed", boom)
    for _ in range(3):
        with eng.begin() as c:  # writes, commits and rollbacks all still work
            c.execute(text("insert into t (v) values ('a')"))
            time.sleep(0.002)
        with eng.connect() as c:
            c.execute(text("insert into t (v) values ('b')"))
            c.rollback()
    with eng.connect() as c:
        assert c.execute(text("select count(*) from t")).scalar() == 3


def test_it_changes_nothing_the_data_is_the_same_with_and_without_it(tmp_path):
    a = make_engine(tmp_path)
    with a.begin() as c:
        c.execute(text("insert into t (v) values ('one'), ('two')"))
        c.execute(text("update t set v = v || '!' where id = 1"))
    with a.connect() as c:
        assert [r[0] for r in c.execute(text("select v from t order by id"))] == ["one!", "two"]


# ---------------------------------------------------------------------------------------------- start-up probe
def test_the_startup_probe_says_once_how_long_the_step_held_the_database(tmp_path, caplog):
    eng = make_engine(tmp_path)
    with caplog.at_level("INFO"):
        with lock_watch.startup_probe("recover_once"):
            for _ in range(3):
                with eng.begin() as c:
                    c.execute(text("insert into t (v) values ('a')"))
    lines = [r.getMessage() for r in caplog.records if r.name == "mba_bot.lock_watch" and r.levelno == logging.INFO]
    assert len(lines) == 1 and lines[0].startswith("start-up recover_once: took ") and "write transactions 3," in lines[0] and "longest single hold" in lines[0]


def test_a_probe_whose_closing_snapshot_fails_still_raises_nothing(monkeypatch):
    calls = {"n": 0}
    real = lock_watch.snapshot

    def flaky():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("broken at the end")
        return real()

    monkeypatch.setattr(lock_watch, "snapshot", flaky)
    with lock_watch.startup_probe("z"):
        pass


def test_the_probe_passes_the_steps_own_exception_through_and_never_raises_itself(monkeypatch):
    with pytest.raises(ValueError):
        with lock_watch.startup_probe("x"):
            raise ValueError("the step failed")
    monkeypatch.setattr(lock_watch, "snapshot", lambda: (_ for _ in ()).throw(RuntimeError("broken")))
    with lock_watch.startup_probe("y"):
        pass  # nothing raised


# ---------------------------------------------------------------------------------------------- the setting
def test_the_setting_is_on_at_three_seconds_and_zero_switches_it_off(monkeypatch):
    monkeypatch.delenv("LOCK_WATCH_SECONDS", raising=False)
    assert config.load_settings().lock_watch_seconds == 3.0
    monkeypatch.setenv("LOCK_WATCH_SECONDS", "0")
    assert config.load_settings().lock_watch_seconds == 0.0
    monkeypatch.setenv("LOCK_WATCH_SECONDS", "-4")
    assert config.load_settings().lock_watch_seconds == 0.0
    monkeypatch.setenv("LOCK_WATCH_SECONDS", "abc")
    assert config.load_settings().lock_watch_seconds == 3.0


def test_init_db_attaches_it_only_when_asked(tmp_path):
    from mba_bot import db as dbmod

    dbmod.init_db(f"sqlite:///{tmp_path}/a.db")
    assert not [1 for _ in dbmod._engine.dispatch.commit]
    dbmod.init_db(f"sqlite:///{tmp_path}/b.db", lock_watch_seconds=3.0)
    assert [1 for _ in dbmod._engine.dispatch.commit]
