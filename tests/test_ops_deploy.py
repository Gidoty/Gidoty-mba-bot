"""The owner-run deploy scripts (ops/deploy/) and the CI workflow (.github/workflows/ci.yml). Everything is local: a bare git repository stands in for the remote, a temporary SQLite file for the database, and small shell
shims for systemctl, journalctl, curl, pip and alembic that only record what they were asked to do. No service is touched, no network is used.

PROVES: the live-trade gate refuses a restart while a live trade is open or the database cannot be read (and nothing is stopped); the expected-head, ancestor (fast-forward only) and clean-tree checks; an
unchanged head does nothing; a dry run changes nothing; the happy path order (stop, backup, merge, import check, start, checks) with alembic and pip only when a migration file or requirements change; the pre-deploy
copy is a real, integrity-checked copy; a failure after the stop puts the old code back and starts the service again; the rollback script (gate, ancestor check, optional database restore with an integrity check
and the current file kept aside); no script prints or reads a secret; every script parses; the workflow runs both suites on every pull request with read-only permissions and no secrets.
DOES NOT PROVE anything on the real server: systemd, Nginx, the real service and the real Binance connection are not involved."""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops" / "deploy"
SECRET = "SUPER-SECRET-VALUE-9f8e7d"


def sh(cmd, cwd=None, env=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, shell=isinstance(cmd, str))
    if check and r.returncode != 0:
        raise AssertionError(f"{cmd} failed: {r.stdout}{r.stderr}")
    return r


class Box:
    pass


@pytest.fixture()
def box(tmp_path, monkeypatch):
    b = Box()
    b.tmp = tmp_path
    origin, server, dev = tmp_path / "origin.git", tmp_path / "app", tmp_path / "dev"
    sh(["git", "init", "-q", "--bare", "-b", "main", str(origin)])
    sh(["git", "clone", "-q", str(origin), str(dev)])
    for c in ("user.email=t@example.com", "user.name=t"):
        sh(["git", "-C", str(dev), "config", c.split("=")[0], c.split("=")[1]])
    (dev / "requirements.txt").write_text("fastapi\n")
    (dev / "alembic" / "versions").mkdir(parents=True)
    (dev / "alembic" / "versions" / "0001_first.py").write_text("# first\n")
    (dev / "README").write_text("one\n")
    sh(["git", "-C", str(dev), "add", "-A"])
    sh(["git", "-C", str(dev), "commit", "-q", "-m", "one"])
    sh(["git", "-C", str(dev), "push", "-q", "origin", "main"])
    sh(["git", "clone", "-q", str(origin), str(server)])
    b.origin, b.server, b.dev = origin, server, dev
    # the database, with the real tables
    b.db = tmp_path / "mba_bot.db"
    from mba_bot import db as dbmod

    dbmod.init_db(f"sqlite:///{b.db}")
    # shims
    shims = tmp_path / "shims"
    shims.mkdir()
    b.calls = tmp_path / "calls.log"
    b.state = tmp_path / "state"
    b.state.write_text("active")
    b.journal = tmp_path / "journal.txt"
    b.journal.write_text("2026-01-01 mba round complete\n")

    def shim(name, body):
        p = shims / name
        p.write_text("#!/bin/bash\n" + textwrap.dedent(body))
        p.chmod(0o755)
        return p

    shim("systemctl", f'''echo "systemctl $*" >> {b.calls}
        case "$1" in stop) echo inactive > {b.state};; start) echo active > {b.state};; is-active) [ "$(cat {b.state})" = active ];; esac''')
    shim("journalctl", f'cat {b.journal}')
    shim("curl", f'[ "$(cat {b.state})" = active ] && printf 200 || printf 000')
    shim("alembic", f'echo "alembic $*" >> {b.calls}; echo "alembic ran with $DATABASE_URL" >> {b.calls}')
    shim("fakepy", f'''export PYTHONPATH="{ROOT / "src"}"
        case "$*" in
          *"-m pip"*) echo "pip $*" >> {b.calls}; exit 0;;
          *"import mba_bot.api.app"*) if [ -f {tmp_path}/fail_import ]; then echo "import broken" >&2; exit 1; fi; exec python3 "$@";;
        esac
        exec python3 "$@"''')
    env = {"PATH": f"{shims}:{os.environ['PATH']}", "HOME": str(tmp_path), "MBA_APP_DIR": str(server), "MBA_PYTHON": str(shims / "fakepy"), "MBA_DB": str(b.db), "MBA_BACKUP_DIR": str(tmp_path / "backups"),
           "MBA_SYSTEMCTL": "systemctl", "MBA_JOURNALCTL": "journalctl", "MBA_CURL": "curl", "MBA_ALEMBIC": str(shims / "alembic"), "MBA_HEARTBEAT_WAIT": "0", "MBA_START_TIMEOUT": "4",
           "SECRET_ENV_FOR_TEST": SECRET, "JWT_SECRET": SECRET, "ENCRYPTION_KEY": SECRET}
    b.env = env
    b.new_commit = lambda files, msg="two": _commit(b, files, msg)
    b.run = lambda script, *args, check=False: sh([str(OPS / script), *args], cwd=str(tmp_path), env=env, check=check)
    b.head = lambda repo: sh(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    b.calls_text = lambda: b.calls.read_text() if b.calls.exists() else ""
    return b


def _commit(b, files, msg):
    for name, content in files.items():
        p = b.dev / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    sh(["git", "-C", str(b.dev), "add", "-A"])
    sh(["git", "-C", str(b.dev), "commit", "-q", "-m", msg])
    sh(["git", "-C", str(b.dev), "push", "-q", "origin", "main"])
    return sh(["git", "-C", str(b.dev), "rev-parse", "HEAD"]).stdout.strip()


def open_live_trade(box):
    from mba_bot.db import session_scope
    from admin_support import mk_account, mk_customer, mk_trade

    with session_scope() as s:
        c = mk_customer(s, "x@example.com", paid=True)
        a = mk_account(s, c)
        mk_trade(s, a, status="open", paper=False, pnl=None)


# ---------------------------------------------------------------- the gate
def test_the_gate_passes_with_no_live_trade_and_refuses_with_one_or_an_unreadable_database(box):
    r = box.run("deploy.sh", "--expect-head", box.head(box.dev), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    open_live_trade(box)
    head = box.new_commit({"README": "two\n"})
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 1 and "live trade is open" in r.stdout and "systemctl stop" not in box.calls_text()
    box.env["MBA_DB"] = str(box.tmp / "missing.db")
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 1 and "could not read" in r.stdout and "systemctl stop" not in box.calls_text()


# ---------------------------------------------------------------- preflight
def test_the_expected_head_must_match_the_remote(box):
    head = box.new_commit({"README": "two\n"})
    r = box.run("deploy.sh", "--expect-head", "deadbeef")
    assert r.returncode == 1 and "not at the expected" in r.stderr and box.calls_text() == ""
    assert box.run("preflight.sh", "nothex").returncode == 1
    assert box.run("preflight.sh", head[:10]).returncode == 0  # a short form of the real head is fine


def test_a_server_that_has_diverged_is_refused_and_nothing_changes(box):
    head = box.new_commit({"README": "two\n"})
    (box.server / "README").write_text("local\n")
    sh(["git", "-C", str(box.server), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-am", "local only"])
    before = box.head(box.server)
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 1 and "not a fast-forward" in r.stderr and box.head(box.server) == before and "systemctl" not in box.calls_text()


def test_modified_tracked_files_are_refused_but_untracked_ones_are_not(box):
    head = box.new_commit({"README": "two\n"})
    (box.server / "notes-untracked.txt").write_text("x")
    assert box.run("preflight.sh", head).returncode == 0
    (box.server / "README").write_text("edited on the server\n")
    r = box.run("preflight.sh", head)
    assert r.returncode == 1 and "modified" in r.stderr


def test_an_unchanged_head_does_nothing(box):
    r = box.run("deploy.sh", "--expect-head", box.head(box.dev))
    assert r.returncode == 0 and "nothing to deploy" in r.stdout and "systemctl" not in box.calls_text()


def test_a_dry_run_reports_the_plan_and_changes_nothing(box):
    head = box.new_commit({"alembic/versions/0002_x.py": "# new\n", "requirements.txt": "fastapi\nnewdep\n"})
    before = box.head(box.server)
    r = box.run("deploy.sh", "--expect-head", head, "--dry-run")
    assert r.returncode == 0 and "DRY RUN" in r.stdout and "install requirements" in r.stdout and "alembic upgrade heads" in r.stdout
    assert box.head(box.server) == before and box.calls_text() == "" and not (box.tmp / "backups").exists()


# ---------------------------------------------------------------- the deploy
def test_the_happy_path_without_a_migration(box):
    old = box.head(box.server)
    head = box.new_commit({"README": "two\n"})
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls_text().splitlines()
    assert [c for c in calls if c.startswith("systemctl") and ("stop" in c or " start" in c)] == ["systemctl stop mba-bot", "systemctl start mba-bot"]
    assert not any(c.startswith("alembic") or c.startswith("pip") for c in calls)
    assert "MIGRATION=no" in r.stdout and "no migration file: alembic not run" in r.stdout and "import check ok" in r.stdout and "service active, health 200" in r.stdout
    assert box.head(box.server) == head and f"DEPLOYED {old[:12]} -> {head[:12]}" in r.stdout and f"rollback.sh {old[:12]}" in r.stdout


def test_the_predeploy_copy_is_a_real_checked_copy_made_after_the_stop(box):
    con = sqlite3.connect(box.db)
    con.execute("create table marker (v text)")
    con.execute("insert into marker values ('kept')")
    con.commit()
    con.close()
    head = box.new_commit({"README": "two\n"})
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 0
    copies = list((box.tmp / "backups").glob("mba_bot-predeploy-*.db"))
    assert len(copies) == 1 and oct(copies[0].stat().st_mode & 0o777) == "0o600"
    c = sqlite3.connect(copies[0])
    assert c.execute("pragma integrity_check").fetchall() == [("ok",)] and c.execute("select v from marker").fetchall() == [("kept",)]
    assert re.search(r"backup ok: \S+ \(\d+ bytes, sha256 [0-9a-f]{64}\)", r.stdout)


def test_alembic_and_pip_run_only_when_a_migration_or_requirements_change(box):
    head = box.new_commit({"alembic/versions/0002_x.py": "# new\n", "requirements.txt": "fastapi\nnewdep\n"})
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls_text()
    assert "pip" in calls and "install --quiet -r requirements.txt" in calls and "alembic upgrade heads" in calls and f"alembic ran with sqlite:///{box.db}" in calls
    order = [i for i, c in enumerate(calls.splitlines()) if c.startswith(("systemctl stop", "pip", "alembic upgrade", "systemctl start"))]
    assert order == sorted(order) and "MIGRATION=yes" in r.stdout


def test_a_failed_import_check_puts_the_old_code_back_and_starts_the_service(box):
    old = box.head(box.server)
    head = box.new_commit({"README": "two\n"})
    (box.tmp / "fail_import").write_text("1")
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 1 and "import check FAILED" in r.stdout and "Putting the code back" in r.stdout and "database was NOT restored" in r.stdout
    assert box.head(box.server) == old and box.state.read_text().strip() == "active"
    calls = box.calls_text().splitlines()
    assert calls[0] == "systemctl stop mba-bot" and "systemctl start mba-bot" in calls and not any(c.startswith("alembic upgrade") for c in calls)
    assert re.search(r"backup ok: \S+", r.stdout)  # the copy was made before the failure and is named


def test_a_service_that_never_gets_healthy_is_rolled_back(box):
    old = box.head(box.server)
    head = box.new_commit({"README": "two\n"})
    box.env["MBA_CURL"] = str(box.tmp / "shims" / "curl_bad")
    (box.tmp / "shims" / "curl_bad").write_text("#!/bin/bash\nprintf 500\n")
    (box.tmp / "shims" / "curl_bad").chmod(0o755)
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 1 and "did not become healthy" in r.stdout and box.head(box.server) == old


def test_a_traceback_in_the_log_after_the_start_fails_the_deploy(box):
    old = box.head(box.server)
    head = box.new_commit({"README": "two\n"})
    box.journal.write_text("round complete\nTraceback (most recent call last):\n")
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 1 and "Traceback or CRITICAL" in r.stdout and box.head(box.server) == old


def test_a_missing_scheduler_round_fails_the_deploy_when_the_check_is_on(box):
    old = box.head(box.server)
    head = box.new_commit({"README": "two\n"})
    box.journal.write_text("nothing useful\n")
    box.env["MBA_HEARTBEAT_WAIT"] = "5"
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 1 and "no scheduler round was logged" in r.stdout and box.head(box.server) == old


# ---------------------------------------------------------------- rollback
def test_rollback_goes_back_one_commit_after_the_gate_and_checks_the_ancestor(box):
    old = box.head(box.server)
    head = box.new_commit({"README": "two\n"})
    assert box.run("deploy.sh", "--expect-head", head).returncode == 0
    r = box.run("rollback.sh", old[:12])
    assert r.returncode == 0 and "ROLLED BACK" in r.stdout and box.head(box.server) == old
    assert box.run("rollback.sh", head[:12]).returncode == 1  # head is no longer an ancestor of the current HEAD
    assert box.run("rollback.sh", "nothex").returncode == 1
    open_live_trade(box)
    assert box.run("rollback.sh", old[:12]).returncode == 1


def test_rollback_restores_the_database_only_when_asked_and_keeps_the_current_file(box):
    con = sqlite3.connect(box.db)
    con.execute("create table marker (v text)")
    con.execute("insert into marker values ('before')")
    con.commit()
    con.close()
    old = box.head(box.server)
    head = box.new_commit({"README": "two\n"})
    r = box.run("deploy.sh", "--expect-head", head)
    backup = re.search(r"backup ok: (\S+)", r.stdout).group(1)
    con = sqlite3.connect(box.db)
    con.execute("insert into marker values ('after')")
    con.commit()
    con.close()
    r = box.run("rollback.sh", old[:12])
    assert r.returncode == 0 and sqlite3.connect(box.db).execute("select count(*) from marker").fetchone() == (2,)  # code only: the data stays
    r = box.run("rollback.sh", old[:12], "--restore-db", backup)
    assert r.returncode == 0 and "database restored" in r.stdout
    assert sqlite3.connect(box.db).execute("select count(*) from marker").fetchone() == (1,)
    assert len(list(box.tmp.glob("mba_bot.db.before-rollback-*"))) == 1
    broken = box.tmp / "broken.db"
    broken.write_bytes(b"not a database" * 100)
    r = box.run("rollback.sh", old[:12], "--restore-db", str(broken))
    assert r.returncode == 1 and "integrity" in r.stderr


# ---------------------------------------------------------------- no secrets, syntax, CI
def test_no_script_prints_or_reads_a_secret(box):
    head = box.new_commit({"README": "two\n"})
    out = ""
    for args in (("deploy.sh", "--expect-head", head), ("rollback.sh", box.head(box.server)[:12]), ("preflight.sh", head), ("backup_check.sh",)):
        r = box.run(*args)
        out += r.stdout + r.stderr
    assert SECRET not in out
    for p in OPS.glob("*.sh"):
        text = p.read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"\.env\b|printenv|\benv\b\s|set -x|set -o xtrace|cat\s+\S*(env|secret)", code), p.name
        assert "sudo" not in code, p.name


@pytest.mark.parametrize("script", sorted(p.name for p in OPS.glob("*.sh")))
def test_every_script_parses_and_is_executable(script):
    assert subprocess.run(["bash", "-n", str(OPS / script)]).returncode == 0
    if script != "common.sh":
        assert os.access(OPS / script, os.X_OK)


def test_the_backup_label_cannot_escape_the_backup_folder(box):
    assert box.run("backup_check.sh", "../evil").returncode == 1
    assert box.run("backup_check.sh", "nightly").returncode == 0


def test_the_ci_workflow_runs_both_suites_on_every_pull_request_with_read_only_permissions():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    triggers = wf.get("on", wf.get(True))
    assert "pull_request" in triggers and triggers["push"]["branches"] == ["main"]
    assert wf["permissions"] == {"contents": "read"}
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pull_request_target" not in text and "secrets." not in text and "deploy" not in text.lower().replace("no deploy", "")
    py = wf["jobs"]["python"]
    runs = " ".join(s.get("run", "") for s in py["steps"])
    assert "pytest" in runs and "requirements-dev.txt" in runs
    fe = " ".join(s.get("run", "") for s in wf["jobs"]["frontend"]["steps"])
    assert "npm ci" in fe and "node --test" in fe and "npm run build" in fe
    assert all(s.get("uses", "").startswith(("actions/checkout@v", "actions/setup-python@v", "actions/setup-node@v")) for j in wf["jobs"].values() for s in j["steps"] if "uses" in s)


def test_the_workflow_runs_every_javascript_test_file_in_the_repo():
    run = next(s["run"] for s in yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["frontend"]["steps"] if "node --test" in s.get("run", ""))
    assert "frontend/src/lib/*.test.js" in run and "frontend/src/*.test.js" in run
    for f in (ROOT / "frontend" / "src").rglob("*.test.js"):
        assert f.parent in (ROOT / "frontend" / "src", ROOT / "frontend" / "src" / "lib"), f


# ---------------------------------------------------------------- extra guards found by mutation testing
def test_a_copy_that_fails_its_integrity_check_stops_the_deploy_and_puts_the_old_code_back(box):
    con = sqlite3.connect(box.db)
    con.execute("create table t(a integer primary key, b text)")
    con.execute("create index ib on t(b)")
    con.executemany("insert into t(b) values (?)", [(f"value-{i}",) for i in range(2000)])
    con.commit()
    con.close()
    raw = box.db.read_bytes()
    chosen = None
    for page in range(1, len(raw) // 4096):  # find a damaged page that still lets the online backup finish but fails the integrity check
        damaged = bytearray(raw)
        damaged[page * 4096 + 100:page * 4096 + 108] = b"\xff" * 8
        probe = box.tmp / "probe.db"
        probe.write_bytes(bytes(damaged))
        try:
            a, b = sqlite3.connect(f"file:{probe}?mode=ro", uri=True), sqlite3.connect(":memory:")
            a.backup(b)
            if b.execute("pragma integrity_check").fetchall() != [("ok",)]:
                chosen = bytes(damaged)
                break
        except Exception:  # noqa: BLE001 - a page that breaks the probe itself is not the case wanted
            continue
    assert chosen is not None
    box.db.write_bytes(chosen)
    old = box.head(box.server)
    head = box.new_commit({"README": "two\n"})
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 1 and "FAILED" in (r.stdout + r.stderr) and box.head(box.server) == old and box.state.read_text().strip() == "active"
    assert box.run("backup_check.sh").returncode == 1  # the standalone script refuses too


def test_a_service_that_does_not_stop_aborts_before_anything_else_happens(box):
    old = box.head(box.server)
    head = box.new_commit({"README": "two\n"})
    stuck = box.tmp / "shims" / "systemctl_stuck"
    stuck.write_text(f'#!/bin/bash\necho "systemctl $*" >> {box.calls}\ncase "$1" in is-active) exit 0;; esac\nexit 0\n')
    stuck.chmod(0o755)
    box.env["MBA_SYSTEMCTL"] = str(stuck)
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 1 and box.head(box.server) == old and not (box.tmp / "backups").exists()


def test_the_gate_is_checked_again_just_before_the_stop(box):
    head = box.new_commit({"README": "two\n"})
    shim = box.tmp / "shims" / "git_race"
    shim.write_text(f'''#!/bin/bash
if [ "$3" = "fetch" ] || [ "$1" = "-C" -a "$3" = "fetch" ]; then
  PYTHONPATH="{ROOT / 'src'}:{ROOT / 'tests'}" DATABASE_URL="sqlite:///{box.db}" python3 -c "
from mba_bot import db as d
from mba_bot.db import session_scope
from admin_support import mk_account, mk_customer, mk_trade
d.init_db('sqlite:///{box.db}')
with session_scope() as s:
    c = mk_customer(s, 'late@example.com', paid=True); a = mk_account(s, c); mk_trade(s, a, status='open', paper=False, pnl=None)
"
fi
exec git "$@"
''')
    shim.chmod(0o755)
    box.env["MBA_GIT"] = str(shim)
    r = box.run("deploy.sh", "--expect-head", head)
    assert r.returncode == 1 and "live trade is open" in r.stdout and "systemctl stop" not in box.calls_text()


def test_a_bad_backup_label_is_refused_with_a_clear_message(box):
    r = box.run("backup_check.sh", "../evil")
    assert r.returncode == 1 and "may only hold letters" in r.stderr and not (box.tmp / "backups").exists()
