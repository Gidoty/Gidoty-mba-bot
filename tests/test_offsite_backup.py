"""Off-server encrypted backups (ops/backup): the extended nightly script, the off-server stage, alerts, the watchdog and the
restore drill.

Everything runs in temporary folders. The Backblaze side is a stand-in `rclone` that writes to a local folder (so these tests prove
OUR control flow, checks and secret handling, NOT Backblaze or rclone behaviour); `sqlite3` is a stand-in that uses Python's
backup interface; `age` is the REAL age program; Telegram and the dead-man service are a local HTTP server.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

OPS = Path(__file__).parent.parent / "ops" / "backup"
sys.path.insert(0, str(OPS))

import mba_alert  # noqa: E402
import mba_common as common  # noqa: E402
import mba_offsite  # noqa: E402
import mba_restore_test  # noqa: E402
import mba_watchdog  # noqa: E402

AGE = shutil.which("age")
AGE_KEYGEN = shutil.which("age-keygen")
pytestmark = pytest.mark.skipif(not AGE or not AGE_KEYGEN, reason="the age program is needed for these tests")

B2_ACCOUNT, B2_KEY = "SENTINELACCOUNT0001", "SENTINELKEY00000000000000000000002"
BOT_TOKEN, DEADMAN_PATH = "123456:SENTINELTOKEN-abcdefgh", "/ping/SENTINELPING-99999"
ENV_SECRET = "ENV_FILE_SECRET_SENTINEL_777"
BUCKET = "mba-test-bucket"
SENTINELS = (B2_ACCOUNT, B2_KEY, BOT_TOKEN, DEADMAN_PATH.rsplit("/", 1)[1], ENV_SECRET)
EPOCH = 1_793_000_000  # a Thursday


SQLITE_SHIM = f"""#!{sys.executable}
import os, sys, sqlite3
db, cmd = sys.argv[1], sys.argv[2]
fail = os.environ.get("FAKE_SQLITE_FAIL")
if cmd.startswith(".backup"):
    if fail == "backup":
        sys.exit(1)
    target = cmd.split("'")[1]
    src = sqlite3.connect(db); dst = sqlite3.connect(target); src.backup(dst); dst.close(); src.close()
elif cmd.strip().rstrip(";").upper() == "PRAGMA INTEGRITY_CHECK":
    print("row 1 missing from index x" if fail == "integrity" else "ok")
else:
    sys.exit(2)
"""

RCLONE_SHIM = f"""#!{sys.executable}
import hashlib, json, os, sys
args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(__file__))
log = open(os.path.join(here, "log.txt")).read().strip()
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps({{"argv": args, "has_b2_env": all(os.environ.get(k) for k in ("RCLONE_CONFIG_B2_TYPE", "RCLONE_CONFIG_B2_ACCOUNT", "RCLONE_CONFIG_B2_KEY"))}}) + "\\n")
if not all(os.environ.get(k) for k in ("RCLONE_CONFIG_B2_TYPE", "RCLONE_CONFIG_B2_ACCOUNT", "RCLONE_CONFIG_B2_KEY")):
    sys.stderr.write("no credentials\\n"); sys.exit(1)
# Real rclone 1.76 rejects flags a command does not have (checked against the real binary): copyto takes none of ours,
# lsjson takes only these three. Anything else, such as --no-check-bucket (not a b2 option), must fail here too.
ALLOWED = {{"copyto": set(), "lsjson": {{"--hash", "--no-modtime", "--files-only"}}}}
bad = [a for a in args[1:] if a.startswith("--") and a not in ALLOWED.get(args[0], set())]
if bad:
    sys.stderr.write("Error: unknown flag: " + bad[0] + "\\n"); sys.exit(2)
root = open(os.path.join(here, "root.txt")).read().strip()
paths = [a for a in args[1:] if not a.startswith("--")]
if args[0] == "copyto":
    if os.path.exists(os.path.join(here, "fail_upload")):
        sys.stderr.write("401 unauthorized for key " + os.environ["RCLONE_CONFIG_B2_KEY"] + "\\n"); sys.exit(1)
    src, dst = paths
    assert dst.startswith("b2:")
    target = os.path.join(root, dst[3:])
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, "wb").write(open(src, "rb").read())
elif args[0] == "lsjson":
    folder = os.path.join(root, paths[0][3:])
    out = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        data = open(os.path.join(folder, name), "rb").read()
        sha = hashlib.sha1(data).hexdigest()
        if os.path.exists(os.path.join(here, "bad_hash")):
            sha = "0" * 40
        out.append({{"Name": name, "Size": len(data), "Hashes": {{"sha1": sha}}}})
    print(json.dumps(out))
else:
    sys.exit(2)
"""


class Hits(http.server.BaseHTTPRequestHandler):
    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        self.server.hits.append((self.command, self.path, body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST = _record

    def log_message(self, *a):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hits)
    server.hits = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    bin_dir, cfg, state, backups, b2root = (tmp_path / n for n in ("bin", "cfg", "state", "backups", "b2"))
    for d in (bin_dir, cfg, state, backups, b2root):
        d.mkdir()
    cfg.chmod(0o700)
    for name, body in (("sqlite3", SQLITE_SHIM), ("rclone", RCLONE_SHIM)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)

    key_file = tmp_path / "private.key"
    out = subprocess.run([AGE_KEYGEN, "-o", str(key_file)], capture_output=True, text=True, check=True)
    public = [l for l in (out.stderr + out.stdout).splitlines() if l.startswith("Public key:")][0].split(": ")[1].strip()
    (cfg / "recipient.txt").write_text(public + "\n")

    def write_secret(name, text):
        p = cfg / name
        p.write_text(text)
        p.chmod(0o600)

    write_secret("b2.env", f"RCLONE_CONFIG_B2_TYPE=b2\nRCLONE_CONFIG_B2_ACCOUNT={B2_ACCOUNT}\nRCLONE_CONFIG_B2_KEY={B2_KEY}\nMBA_BACKUP_BUCKET={BUCKET}\n")
    write_secret("alert.env", f"OPS_BOT_TOKEN={BOT_TOKEN}\nOPS_CHAT_ID=424242\nDEADMAN_URL=http://127.0.0.1:{port}{DEADMAN_PATH}\n")

    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    con.executescript("""
        create table customers(id integer primary key, email text);
        create table trading_accounts(id integer primary key, customer_id integer references customers(id));
        create table trades(id integer primary key, account_id integer references trading_accounts(id), pnl real);
        create table kill_switch(id integer primary key, engaged integer);
        create table alembic_version(version_num text);
        insert into alembic_version values ('e5b9c2d7f1a4');
        insert into kill_switch values (1, 0);
    """)
    con.executemany("insert into customers(email) values (?)", [(f"c{i}@example.test",) for i in range(30)])
    con.executemany("insert into trading_accounts(customer_id) values (?)", [(i + 1,) for i in range(30)])
    con.executemany("insert into trades(account_id, pnl) values (?, ?)", [(1 + i % 30, i * 0.1) for i in range(300)])
    con.commit()
    con.close()

    log = tmp_path / "rclone.log"
    (bin_dir / "log.txt").write_text(str(log))
    (bin_dir / "root.txt").write_text(str(b2root))
    values = {
        "MBA_DB": str(db), "MBA_BACKUP_DIR": str(backups), "MBA_BACKUP_STATE_DIR": str(state), "MBA_BACKUP_CONFIG_DIR": str(cfg),
        "MBA_OPS_DIR": str(OPS), "MBA_PYTHON3": sys.executable, "MBA_RCLONE_BIN": str(bin_dir / "rclone"), "MBA_AGE_BIN": AGE,
        "MBA_ALERT_API_BASE": f"http://127.0.0.1:{port}",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)

    class E:
        pass

    e = E()
    e.tmp, e.bin, e.cfg, e.state, e.backups, e.b2root, e.db, e.key, e.public, e.log, e.server, e.port = (
        tmp_path, bin_dir, cfg, state, backups, b2root, db, key_file, public, log, server, port)
    e.values = values
    e.write_secret = write_secret
    yield e
    server.shutdown()


def script_env(e, **extra):
    env = {**os.environ, **extra}
    env["PATH"] = f"{e.bin}:/usr/bin:/bin"  # cron's minimal PATH, plus the stand-in sqlite3
    return env


def toggle(e, name):
    (e.bin / name).write_text("1")


def run_script(e, **extra):
    return subprocess.run([str(OPS / "backup_db.sh")], env=script_env(e, **extra), capture_output=True, text=True, timeout=120)


def b2_files(e):
    root = e.b2root / BUCKET
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


def rclone_calls(e):
    return [json.loads(l) for l in e.log.read_text().splitlines()] if e.log.exists() else []


def alerts(e):
    return [(p, b) for (m, p, b) in e.server.hits if "/sendMessage" in p]


def pings(e):
    return [p for (m, p, b) in e.server.hits if p == DEADMAN_PATH]


def new_backup(e, name="mba_bot-20261106-030001.db"):
    dst = e.backups / name
    src, out = sqlite3.connect(e.db), sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()
    return dst


def decrypt(e, enc_path, out_db):
    gz = out_db.with_suffix(".gz")
    subprocess.run([AGE, "-d", "-i", str(e.key), "-o", str(gz), str(enc_path)], check=True)
    import gzip

    out_db.write_bytes(gzip.open(gz, "rb").read())
    return sqlite3.connect(f"file:{out_db}?mode=ro", uri=True)


def everything_the_run_wrote(e, proc=None):
    """All text a secret could leak into: process output, state files, the bucket's names and manifests, tool arguments, alert bodies."""
    parts = []
    if proc is not None:
        parts += [proc.stdout, proc.stderr]
    for p in e.state.rglob("*"):
        if p.is_file():
            parts.append(p.read_text(errors="replace"))
    parts += b2_files(e)
    for p in (e.b2root / BUCKET).rglob("*.manifest.json") if (e.b2root / BUCKET).exists() else []:
        parts.append(p.read_text())
    parts += [json.dumps(c["argv"]) for c in rclone_calls(e)]
    parts += [b for (_p, b) in alerts(e)]
    return "\n".join(parts)


# ------------------------------------------------------------------ the extended nightly script, end to end


def test_the_script_keeps_the_old_behaviour_and_ships_one_verified_encrypted_copy(env):
    old = env.backups / "mba_bot-20200101-000000.db"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    env_pre = env.backups / "env.pre-20261105"
    env_pre.write_text(f"JWT_SECRET={ENV_SECRET}\n")
    os.utime(env_pre, (time.time() - 400 * 86400,) * 2)

    proc = run_script(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    copies = sorted(p.name for p in env.backups.glob("mba_bot-*.db"))
    assert len(copies) == 1 and "mba_bot-20200101-000000.db" not in copies  # a fresh copy was made, the 40-day-old one was pruned
    assert env_pre.exists() and env_pre.read_text() == f"JWT_SECRET={ENV_SECRET}\n"  # the prune pattern never touches env.pre-*
    assert (env.state / "last_local_success").exists() and (env.state / "last_offsite_success").exists()

    files = b2_files(env)
    assert len([f for f in files if f.startswith("mba/recent/") and f.endswith(".gz.age")]) == 1
    assert len([f for f in files if f.startswith("mba/recent/") and f.endswith(".manifest.json")]) == 1
    assert any(f.startswith("mba/daily/") for f in files)  # the first success of the UTC day is promoted
    enc = env.b2root / BUCKET / [f for f in files if f.startswith("mba/recent/") and f.endswith(".gz.age")][0]
    restored = decrypt(env, enc, env.tmp / "restored.db")
    assert restored.execute("select count(*) from trades").fetchone()[0] == 300
    assert restored.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert len(pings(env)) == 1 and alerts(env) == []
    # nothing in the backups folder was read by the upload except the one new copy; the env.pre file is in no tool argument or name
    assert all(env_pre.name not in json.dumps(c["argv"]) for c in rclone_calls(env))
    assert ENV_SECRET not in everything_the_run_wrote(env, proc)


def test_the_original_commands_are_still_in_the_script_verbatim():
    text = (OPS / "backup_db.sh").read_text()
    for line in (
        'sqlite3 "$DB" ".backup \'$BACKUP_DIR/mba_bot-$TIMESTAMP.db\'"',
        'sqlite3 "$BACKUP_DIR/mba_bot-$TIMESTAMP.db" "PRAGMA integrity_check;" | grep -qx "ok"',
        'find "$BACKUP_DIR" -type f -name "mba_bot-*.db" -mtime +30 -delete',
        "set -e",
    ):
        assert line in text, line
    assert "set -o pipefail" not in text and "set -u" not in text  # would change how the original pipeline behaves
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "rm " not in code and "-delete" in code and code.count("-delete") == 1  # the only pruner is the original find


def test_a_failed_local_backup_alerts_once_with_its_step_keeps_the_exit_status_and_uploads_nothing(env):
    proc = run_script(env, FAKE_SQLITE_FAIL="backup")
    assert proc.returncode != 0
    msgs = alerts(env)
    assert len(msgs) == 1 and "step%3A+local_backup" in msgs[0][1]
    assert rclone_calls(env) == [] and b2_files(env) == []
    assert not (env.state / "last_local_success").exists()


def test_a_failed_integrity_check_stops_before_any_upload_and_alerts(env):
    proc = run_script(env, FAKE_SQLITE_FAIL="integrity")
    assert proc.returncode != 0 and rclone_calls(env) == [] and b2_files(env) == []
    assert not (env.state / "last_local_success").exists()  # a copy that failed its check is never marked good
    assert len(alerts(env)) == 1 and "integrity_check" in alerts(env)[0][1]


def test_a_failed_upload_alerts_exactly_once_keeps_the_local_copy_and_does_not_mark_success(env):
    old = env.backups / "mba_bot-20200101-000000.db"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    toggle(env, "fail_upload")
    proc = run_script(env)
    assert proc.returncode == mba_offsite.EXIT_ALERTED
    assert len(alerts(env)) == 1 and "step%3A+upload" in alerts(env)[0][1]  # one alert, from the stage that knew the step; the trap added none
    assert list(env.backups.glob("mba_bot-*.db")) and (env.state / "last_local_success").exists()
    assert not old.exists()  # the local prune ran BEFORE the off-server step, so a failed upload never leaves the folder unpruned
    assert not (env.state / "last_offsite_success").exists() and pings(env) == []
    # the tool's error text contained the key; the saved copy of it must not
    saved = (env.state / "offsite_last_error.txt").read_text()
    assert B2_KEY not in saved and "[redacted]" in saved
    assert B2_KEY not in everything_the_run_wrote(env, proc)


def test_a_remote_copy_that_does_not_match_is_a_failure_not_a_success(env):
    toggle(env, "bad_hash")
    proc = run_script(env)
    assert proc.returncode == mba_offsite.EXIT_ALERTED and "step%3A+verify" in alerts(env)[0][1]
    assert not (env.state / "last_offsite_success").exists()


def test_an_unreachable_alert_service_never_changes_the_exit_status_or_hides_the_error(env, monkeypatch):
    env.values["MBA_ALERT_API_BASE"] = "http://127.0.0.1:9"  # nothing listens there
    proc = run_script(env, MBA_ALERT_API_BASE="http://127.0.0.1:9", FAKE_SQLITE_FAIL="backup")
    assert proc.returncode == 1  # the original failure of the sqlite3 stand-in, not the alert's


def test_secrets_never_appear_in_output_files_names_arguments_or_messages(env):
    ok = run_script(env)
    toggle(env, "fail_upload")
    bad = run_script(env)
    for proc in (ok, bad):
        assert not any(s in everything_the_run_wrote(env, proc) for s in SENTINELS)
    assert all(call["has_b2_env"] for call in rclone_calls(env))  # the credentials reached rclone through its environment
    assert not any("--no-check-bucket" in call["argv"] for call in rclone_calls(env))  # not a b2 flag in rclone 1.76


def test_the_plain_copy_is_gone_afterwards_and_the_work_folder_is_removed(env, monkeypatch):
    before = set(os.listdir("/dev/shm")) if os.path.isdir("/dev/shm") else set()
    new_backup(env)
    assert mba_offsite.run(str(env.backups / "mba_bot-20261106-030001.db"), now=EPOCH) == 0
    after = set(os.listdir("/dev/shm")) if os.path.isdir("/dev/shm") else set()
    assert not {n for n in after - before if n.startswith("mba-backup-")}
    assert not list(env.state.glob("work-*"))


# ------------------------------------------------------------------ the off-server stage refuses anything but one database


@pytest.mark.parametrize("make", ["env_pre", "wrong_name", "symlink", "long_symlink", "not_sqlite", "tiny", "elsewhere", "dotdot", "missing"])
def test_only_one_named_database_can_be_sent(env, make):
    secret = env.backups / "env.pre-20261105"
    secret.write_text(f"ENCRYPTION_KEY={ENV_SECRET}\n" * 40)
    good = new_backup(env)
    if make == "env_pre":
        target = secret
    elif make == "wrong_name":
        target = env.backups / "evil.db"
        shutil.copy(good, target)
    elif make == "symlink":
        target = env.backups / "mba_bot-20261106-040000.db"
        target.symlink_to(good)
    elif make == "long_symlink":  # a link whose own size passes the size check, so only the link check can stop it
        deep = env.tmp / ("a" * 200) / ("b" * 200) / ("c" * 100)
        deep.mkdir(parents=True)
        real = deep / "valid.db"
        shutil.copy(good, real)
        target = env.backups / "mba_bot-20261106-045000.db"
        target.symlink_to(real)
        assert os.lstat(target).st_size >= 512
    elif make == "not_sqlite":
        target = env.backups / "mba_bot-20261106-050000.db"
        target.write_bytes(b"x" * 4096)
    elif make == "tiny":
        target = env.backups / "mba_bot-20261106-060000.db"
        target.write_bytes(b"SQLite format 3\x00")
    elif make == "elsewhere":
        target = env.tmp / "mba_bot-20261106-070000.db"
        shutil.copy(good, target)
    elif make == "dotdot":
        target = env.backups / ".." / env.backups.name / good.name
        # a path that resolves to the right file is fine; one that names the folder differently is not
        target = env.backups / "sub" / ".." / "env.pre-20261105"
    else:
        target = env.backups / "mba_bot-20261106-080000.db"
    assert mba_offsite.run(str(target), now=EPOCH) == mba_offsite.EXIT_ALERTED
    assert rclone_calls(env) == [] and b2_files(env) == []
    assert len(alerts(env)) == 1 and "step%3A+validate" in alerts(env)[0][1]
    assert secret.read_text().startswith("ENCRYPTION_KEY=")  # untouched


def test_the_off_server_stage_deletes_and_creates_nothing_in_the_backups_folder(env):
    new_backup(env)
    (env.backups / "env.pre-1").write_text("x")
    before = sorted(os.listdir(env.backups))
    assert mba_offsite.run(str(env.backups / "mba_bot-20261106-030001.db"), now=EPOCH) == 0
    assert sorted(os.listdir(env.backups)) == before


def test_a_database_without_the_expected_tables_or_a_damaged_one_is_refused(env):
    empty = env.backups / "mba_bot-20261106-090000.db"
    con = sqlite3.connect(empty)
    con.execute("create table notes(x)")
    con.executemany("insert into notes values (?)", [("x" * 200,)] * 100)
    con.commit()
    con.close()
    assert mba_offsite.run(str(empty), now=EPOCH) == mba_offsite.EXIT_ALERTED and "step%3A+recheck" in alerts(env)[-1][1]
    damaged = new_backup(env, "mba_bot-20261106-100000.db")
    data = bytearray(damaged.read_bytes())
    for i in range(4096 + 100, 4096 * 2):  # corrupt the contents of the second page
        data[i] = 0xFF
    damaged.write_bytes(bytes(data))
    assert mba_offsite.run(str(damaged), now=EPOCH) == mba_offsite.EXIT_ALERTED
    assert b2_files(env) == []


def test_a_copy_under_half_the_last_good_size_is_refused_unless_accepted(env):
    (env.state / "last_offsite.json").write_text(json.dumps({"size_plain": 400000}))
    small = new_backup(env)
    assert os.path.getsize(small) < 200000
    assert mba_offsite.run(str(small), now=EPOCH) == mba_offsite.EXIT_ALERTED and b2_files(env) == []
    assert mba_offsite.run(str(small), now=EPOCH, accept_shrink=True) == 0 and b2_files(env)


def test_a_wrong_key_cannot_open_the_backup_and_the_server_holds_no_private_key(env):
    new_backup(env)
    assert mba_offsite.run(str(env.backups / "mba_bot-20261106-030001.db"), now=EPOCH) == 0
    enc = env.b2root / BUCKET / [f for f in b2_files(env) if f.startswith("mba/recent/") and f.endswith(".gz.age")][0]
    other = env.tmp / "other.key"
    subprocess.run([AGE_KEYGEN, "-o", str(other)], capture_output=True, check=True)
    assert subprocess.run([AGE, "-d", "-i", str(other), "-o", str(env.tmp / "x"), str(enc)], capture_output=True).returncode != 0
    config_text = "".join(p.read_text() for p in env.cfg.iterdir())
    assert "AGE-SECRET-KEY" not in config_text and "AGE-SECRET-KEY" not in everything_the_run_wrote(env)


def test_the_recipient_file_must_be_a_public_key_never_a_private_one(env):
    private = env.key.read_text().split("AGE-SECRET-KEY-")[1].split()[0]
    (env.cfg / "recipient.txt").write_text("AGE-SECRET-KEY-" + private + "\n")
    new_backup(env)
    assert mba_offsite.run(str(env.backups / "mba_bot-20261106-030001.db"), now=EPOCH) == mba_offsite.EXIT_ALERTED
    assert "step%3A+config" in alerts(env)[0][1] and rclone_calls(env) == []


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_a_secret_file_readable_by_others_is_refused(env, mode):
    (env.cfg / "b2.env").chmod(mode)
    new_backup(env)
    assert mba_offsite.run(str(env.backups / "mba_bot-20261106-030001.db"), now=EPOCH) == mba_offsite.EXIT_ALERTED
    assert rclone_calls(env) == []


# ------------------------------------------------------------------ tiers


def utc(y, m, d, h=3, mi=0):
    import calendar

    return calendar.timegm((y, m, d, h, mi, 0))


def tiers_after(env, when):
    before = set(b2_files(env))
    new_backup(env, f"mba_bot-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(when))}.db")
    assert mba_offsite.run(str(env.backups / f"mba_bot-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(when))}.db"), now=when) == 0
    new = set(b2_files(env)) - before
    return {f.split("/")[1] for f in new}


def test_every_run_goes_to_recent_the_first_of_each_utc_day_also_to_daily(env):
    assert tiers_after(env, utc(2026, 11, 4, 3)) == {"recent", "daily"}  # a Wednesday
    assert tiers_after(env, utc(2026, 11, 4, 9)) == {"recent"}  # the second run the same day
    assert tiers_after(env, utc(2026, 11, 5, 9)) == {"recent", "daily"}  # the 03:00 run failed that day: the next success is the daily


def test_sundays_add_weekly_and_the_first_of_the_month_adds_monthly(env):
    assert tiers_after(env, utc(2026, 11, 8, 3)) == {"recent", "daily", "weekly"}  # Sunday 8 Nov 2026
    assert tiers_after(env, utc(2026, 12, 1, 3)) == {"recent", "daily", "monthly"}  # Tuesday 1 Dec
    assert tiers_after(env, utc(2026, 11, 1, 3)) == {"recent", "daily", "weekly", "monthly"}  # Sunday the 1st: both


def test_a_failed_extra_tier_upload_does_not_advance_the_daily_marker(env, monkeypatch):
    when = utc(2026, 11, 4, 3)
    new_backup(env, "mba_bot-20261104-030000.db")

    class FailOnDaily(mba_offsite.Runner):
        def run(self, argv, *, env, timeout):
            if argv[1] == "copyto" and "/daily/" in argv[-1]:
                return 1, "", "boom"
            return super().run(argv, env=env, timeout=timeout)

    assert mba_offsite.run(str(env.backups / "mba_bot-20261104-030000.db"), now=when, runner=FailOnDaily()) == mba_offsite.EXIT_ALERTED
    assert not (env.state / "last_daily_date").exists() and not (env.state / "last_offsite_success").exists()
    assert any(f.startswith("mba/recent/") for f in b2_files(env))  # the recent copy had already been verified: there is an off-server copy


# ------------------------------------------------------------------ alerts and the watchdog


def test_alert_text_is_fixed_and_unknown_steps_are_not_echoed():
    assert "secret" not in mba_alert.failure_text("upload").lower()
    assert "step: unknown" in mba_alert.failure_text("not-a-step; rm -rf /")
    assert "rm -rf" not in mba_alert.failure_text("not-a-step; rm -rf /")


def test_an_unsafe_alert_or_ping_endpoint_is_refused(env, monkeypatch):
    other = http.server.ThreadingHTTPServer(("127.0.0.2", 0), Hits)  # reachable, but not the one address plain http may use
    other.hits = []
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("MBA_ALERT_API_BASE", f"http://127.0.0.2:{other.server_address[1]}")
        assert mba_alert.send_telegram("hello") is False
        for bad in ("http://evil.example/", "ftp://x/", "file:///etc/passwd"):
            monkeypatch.setenv("MBA_ALERT_API_BASE", bad)
            assert mba_alert.send_telegram("hello") is False
        env.write_secret("alert.env", f"OPS_BOT_TOKEN=abc:def\nOPS_CHAT_ID=1\nDEADMAN_URL=http://127.0.0.2:{other.server_address[1]}/p\n")
        assert mba_alert.ping_deadman() is False
        assert other.hits == [] and not [h for h in env.server.hits if "hello" in h[2]]  # nothing was sent anywhere
    finally:
        other.shutdown()


def mark(e, name, epoch):
    (e.state / name).write_text(f"{epoch}\n")


def test_the_watchdog_is_silent_before_the_first_run_and_when_everything_is_fresh(env):
    assert mba_watchdog.run(now=EPOCH, state=str(env.state)) == 0 and alerts(env) == []  # not armed: nothing has run to be late
    for name in ("last_local_success", "last_offsite_success", "last_daily_success"):
        mark(env, name, EPOCH - 3600)
    assert mba_watchdog.run(now=EPOCH, state=str(env.state)) == 0 and alerts(env) == []


@pytest.mark.parametrize("stale,code", [("last_offsite_success", "off-server upload"), ("last_local_success", "local backup")])
def test_the_watchdog_alerts_when_a_six_hourly_marker_is_older_than_eight_hours(env, stale, code):
    for name in ("last_local_success", "last_offsite_success", "last_daily_success"):
        mark(env, name, EPOCH - 3600)
    mark(env, stale, EPOCH - 8 * 3600 - 60)
    mba_watchdog.run(now=EPOCH, state=str(env.state))
    assert len(alerts(env)) == 1 and code.replace(" ", "+") in alerts(env)[0][1]
    mark(env, stale, EPOCH - 7 * 3600)  # 7 hours is still fine
    for f in env.state.glob("watchdog_state.json"):
        f.unlink()
    env.server.hits.clear()
    mba_watchdog.run(now=EPOCH, state=str(env.state))
    assert alerts(env) == []


def test_the_daily_limit_is_twenty_six_hours_and_alerts_repeat_at_most_every_six(env):
    for name in ("last_local_success", "last_offsite_success"):
        mark(env, name, EPOCH - 600)
    mark(env, "last_daily_success", EPOCH - 25 * 3600)
    mba_watchdog.run(now=EPOCH, state=str(env.state))
    assert alerts(env) == []
    mark(env, "last_daily_success", EPOCH - 26 * 3600 - 60)
    mba_watchdog.run(now=EPOCH, state=str(env.state))
    assert len(alerts(env)) == 1
    mba_watchdog.run(now=EPOCH + 3600, state=str(env.state))  # an hour later: no repeat
    assert len(alerts(env)) == 1
    mark(env, "last_local_success", EPOCH + 5 * 3600 - 600)
    mark(env, "last_offsite_success", EPOCH + 5 * 3600 - 600)
    mba_watchdog.run(now=EPOCH + 6 * 3600 + 60, state=str(env.state))  # six hours on, still stale: repeats
    assert len(alerts(env)) == 2


def test_the_watchdog_says_when_the_problem_clears_and_sends_one_ok_message_on_sunday(env):
    for name in ("last_local_success", "last_offsite_success", "last_daily_success"):
        mark(env, name, EPOCH - 30 * 3600)
    mba_watchdog.run(now=EPOCH, state=str(env.state))
    assert len(alerts(env)) == 1  # one message for the three stale markers
    body = alerts(env)[0][1]
    assert "local+backup" in body and "off-server+upload" in body and "daily+off-server+copy" in body
    for name in ("last_local_success", "last_offsite_success", "last_daily_success"):
        mark(env, name, EPOCH + 3600)
    mba_watchdog.run(now=EPOCH + 7200, state=str(env.state))
    assert len(alerts(env)) == 2 and "cleared" in alerts(env)[-1][1]
    sunday = utc(2026, 11, 8, 10)
    for name in ("last_local_success", "last_offsite_success", "last_daily_success"):
        mark(env, name, sunday - 3600)
    mba_watchdog.run(now=sunday, state=str(env.state))
    mba_watchdog.run(now=sunday + 3600, state=str(env.state))
    oks = [a for a in alerts(env) if "backups+OK" in a[1]]
    assert len(oks) == 1  # once per week
    before = len(alerts(env))
    mba_watchdog.run(now=utc(2026, 11, 8, 8), state=str(env.state))  # before 09:00 UTC: not on that day's schedule (and already sent for the week)
    assert len(alerts(env)) == before


def test_a_never_succeeded_offsite_upload_is_reported_once_the_local_job_has_run(env):
    mark(env, "last_local_success", EPOCH - 100)
    mba_watchdog.run(now=EPOCH, state=str(env.state))
    texts = " ".join(a[1] for a in alerts(env))
    assert "off-server+upload+has+ever" in texts or "has+ever+succeeded" in texts


# ------------------------------------------------------------------ the restore drill


def test_the_restore_drill_round_trips_a_backup_and_removes_its_scratch_folder(env, capsys):
    new_backup(env)
    assert mba_offsite.run(str(env.backups / "mba_bot-20261106-030001.db"), now=utc(2026, 11, 6, 3)) == 0
    scratch_before = set(os.listdir(tempdir()))
    assert mba_restore_test.main(["--key", str(env.key), "--from-dir", str(env.b2root / BUCKET), "--tier", "daily"]) == 0
    out = capsys.readouterr().out
    assert "RESTORE TEST PASSED" in out and "rows in trades: 300" in out and "rows in customers: 30" in out and "alembic version: e5b9c2d7f1a4" in out
    assert "c1@example.test" not in out  # counts only, never row contents
    assert not {n for n in set(os.listdir(tempdir())) - scratch_before if n.startswith("mba-restore-")}


def tempdir():
    import tempfile

    return tempfile.gettempdir()


def test_the_restore_drill_fails_loudly_on_a_tampered_backup(env, capsys):
    new_backup(env)
    assert mba_offsite.run(str(env.backups / "mba_bot-20261106-030001.db"), now=utc(2026, 11, 6, 3)) == 0
    enc = next(p for p in (env.b2root / BUCKET / "mba" / "daily").iterdir() if p.name.endswith(".gz.age"))
    enc.write_bytes(enc.read_bytes()[:-5] + b"XXXXX")
    with pytest.raises(SystemExit) as stop:
        mba_restore_test.main(["--key", str(env.key), "--from-dir", str(env.b2root / BUCKET), "--tier", "daily"])
    assert "CHECKSUM MISMATCH" in str(stop.value)


# ------------------------------------------------------------------ documentation that the owner will follow stays consistent


def test_the_runbook_matches_the_decisions_and_contains_no_secret():
    text = (OPS / "README.md").read_text()
    for needle in ("0 3,9,15,21 * * *", "read -rs", "env.pre", "recent/", "daily/", "weekly/", "monthly/", "restore"):
        assert needle in text, needle
    assert "AGE-SECRET-KEY-1" not in text and "keyID" not in text.replace("key ID", "")
    for line in text.splitlines():
        assert not line.strip().startswith("RCLONE_CONFIG_B2_KEY=") or "<" in line  # only placeholders
