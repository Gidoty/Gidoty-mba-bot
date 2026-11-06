#!/usr/bin/env python3
"""Off-server stage of the database backup: check, compress, encrypt, upload, verify. Standard library plus the `age` and
`rclone` programs.

    mba_offsite.py /opt/mba-bot/backups/mba_bot-YYYYMMDD-HHMMSS.db [--accept-shrink] [--now EPOCH]
    mba_offsite.py --check-config

It takes exactly ONE file path. It never lists or globs the backups folder, so the env.pre-* files that live near the
backups cannot be sent. The path must be a regular file directly inside the backups folder named mba_bot-YYYYMMDD-HHMMSS.db
with the SQLite header. It deletes nothing locally (the nightly script is the only pruner). Off-server retention is done by
Backblaze lifecycle rules, so the upload key needs no delete permission.

Exit status: 0 success; 3 failed AND already alerted (the shell wrapper then sends no second alert); 2 usage error.
The plain database copy only ever exists in a private (mode 700) memory-backed folder and is removed afterwards. The server holds
only the age PUBLIC key, so it cannot decrypt anything it uploaded. No secret is printed, logged or passed on a command line:
the Backblaze credentials reach rclone through its environment, read from /etc/mba-backup/b2.env.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse

import mba_alert as alert
import mba_common as common

SOURCE_NAME = re.compile(r"^mba_bot-[0-9]{8}-[0-9]{6}\.db$")
SQLITE_HEADER = b"SQLite format 3\x00"
REQUIRED_TABLES = {"customers", "trading_accounts", "trades"}
SHRINK_LIMIT = 0.5          # refuse a copy under 50% of the last good one (unless --accept-shrink)
SHRINK_FLOOR_BYTES = 65536  # ... but only once the previous copy was bigger than this
EXIT_OK, EXIT_ALERTED, EXIT_USAGE = 0, 3, 2


class StepError(Exception):
    """A failed step. Carries only the step name (from a fixed list); the reason is logged separately and scrubbed."""

    def __init__(self, step: str, detail: str = ""):
        super().__init__(step)
        self.step = step
        self.detail = detail


# ------------------------------------------------------------------ source checks


def validate_source(path: str, backups_dir: str) -> str:
    p = os.path.abspath(path)
    if os.path.dirname(p) != os.path.abspath(backups_dir):
        raise StepError("validate", "the file is not directly inside the backups folder")
    name = os.path.basename(p)
    if not SOURCE_NAME.match(name):
        raise StepError("validate", "the file name is not mba_bot-YYYYMMDD-HHMMSS.db")
    try:
        info = os.lstat(p)
    except OSError:
        raise StepError("validate", "the file does not exist")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or os.path.realpath(p) != p:
        raise StepError("validate", "the file is a link or not a regular file")
    if info.st_size < 512:
        raise StepError("validate", "the file is too small to be a database")
    with open(p, "rb") as handle:
        if handle.read(16) != SQLITE_HEADER:
            raise StepError("validate", "the file does not have the SQLite header")
    return p


def recheck(path: str) -> dict:
    """Read-only checks on the copy. Returns facts for the manifest. Opens with immutable=1 so nothing is created or changed."""
    try:
        con = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro&immutable=1", uri=True, timeout=5)
        try:
            if con.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise StepError("recheck", "integrity_check did not return ok")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            missing = REQUIRED_TABLES - tables
            if missing:
                raise StepError("recheck", f"{len(missing)} required table(s) missing")
            for table in sorted(REQUIRED_TABLES):
                con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()
            alembic = None
            if "alembic_version" in tables:
                row = con.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
                alembic = row[0] if row else None
            return {"alembic_version": alembic, "tables": len(tables)}
        finally:
            con.close()
    except StepError:
        raise
    except sqlite3.Error as exc:
        raise StepError("recheck", f"sqlite error ({type(exc).__name__})")


def check_shrink(size_plain: int, state: str, accept_shrink: bool) -> None:
    try:
        last = json.loads(common.read_text(os.path.join(state, "last_offsite.json")) or "null")
    except ValueError:
        last = None
    previous = int(last.get("size_plain", 0)) if isinstance(last, dict) else 0
    if previous > SHRINK_FLOOR_BYTES and size_plain < previous * SHRINK_LIMIT and not accept_shrink:
        raise StepError("recheck", "the copy is less than half the size of the last good upload (use --accept-shrink if that is intended)")


# ------------------------------------------------------------------ running other programs


class Runner:
    """Runs age and rclone. Secrets travel only in `env`; the argument lists never contain a secret. Output is returned for the
    caller to scrub, and is never printed."""

    def run(self, argv: list[str], *, env: dict[str, str], timeout: int) -> tuple[int, str, str]:
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout, check=False)
        return done.returncode, done.stdout, done.stderr


def base_env() -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C"}


# ------------------------------------------------------------------ helpers


def pick_workdir(needed: int, state: str) -> str:
    shm = "/dev/shm"
    if os.path.isdir(shm) and os.access(shm, os.W_OK) and shutil.disk_usage(shm).free > needed * 3:
        return tempfile.mkdtemp(prefix="mba-backup-", dir=shm)
    os.makedirs(state, mode=0o700, exist_ok=True)
    return tempfile.mkdtemp(prefix="work-", dir=state)


def file_digests(path: str) -> tuple[str, str]:
    sha256, sha1 = hashlib.sha256(), hashlib.sha1()  # noqa: S324 - B2 reports SHA-1; used to compare, not to protect
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha256.update(chunk)
            sha1.update(chunk)
    return sha256.hexdigest(), sha1.hexdigest()


def choose_tiers(now: float, state: str) -> list[str]:
    """recent always. daily = the first successful run of the UTC day (normally the 03:00 run). weekly and monthly ride on that
    daily run: weekly on a Sunday, monthly on the 1st."""
    tm = time.gmtime(now)
    tiers = ["recent"]
    if common.read_text(os.path.join(state, "last_daily_date")) != time.strftime("%Y-%m-%d", tm):
        tiers.append("daily")
        if tm.tm_wday == 6:
            tiers.append("weekly")
        if tm.tm_mday == 1:
            tiers.append("monthly")
    return tiers


# ------------------------------------------------------------------ the run


def run(source: str, *, now: float | None = None, runner: Runner | None = None, accept_shrink: bool = False) -> int:
    now = time.time() if now is None else now
    runner = runner or Runner()
    state = common.state_dir()
    secrets: list[str] = []
    workdir = None
    try:
        try:
            b2 = common.parse_env_file(os.path.join(common.config_dir(), "b2.env"), common.B2_ENV_KEYS, required=common.B2_ENV_KEYS)
            secrets += [b2["RCLONE_CONFIG_B2_ACCOUNT"], b2["RCLONE_CONFIG_B2_KEY"]]
            if not common.BUCKET_NAME.match(b2["MBA_BACKUP_BUCKET"]) or b2["RCLONE_CONFIG_B2_TYPE"] != "b2":
                raise common.ConfigError("b2.env has an invalid bucket name or type")
            recipient = common.read_recipient(os.path.join(common.config_dir(), "recipient.txt"))
            age_bin = common.find_binary("age", "MBA_AGE_BIN")
            rclone_bin = common.find_binary("rclone", "MBA_RCLONE_BIN")
        except common.ConfigError as exc:
            raise StepError("config", str(exc))

        path = validate_source(source, common.backup_dir())
        facts = recheck(path)
        size_plain = os.path.getsize(path)
        check_shrink(size_plain, state, accept_shrink)

        workdir = pick_workdir(size_plain, state)
        name = os.path.basename(path)
        stamp = common.utc_stamp(now)
        obj = f"{stamp}-{name}.gz.age"
        gz, enc = os.path.join(workdir, name + ".gz"), os.path.join(workdir, obj)
        try:
            with open(path, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        except OSError:
            raise StepError("compress", "could not compress the copy")
        code, _out, err = runner.run([age_bin, "-r", recipient, "-o", enc, gz], env=base_env(), timeout=600)
        os.unlink(gz)
        if code != 0 or not os.path.isfile(enc) or os.path.getsize(enc) == 0:
            raise StepError("encrypt", f"age exited with status {code}")
        with open(enc, "rb") as handle:
            if not handle.read(32).startswith(b"age-encryption.org/v1"):
                raise StepError("encrypt", "the encrypted file does not have the age header")
        sha256, sha1 = file_digests(enc)
        tiers = choose_tiers(now, state)
        manifest = {"source": name, "object": obj, "created_utc": stamp, "size_plain": size_plain, "size_encrypted": os.path.getsize(enc),
                    "sha256_encrypted": sha256, "alembic_version": facts["alembic_version"], "tiers": tiers, "tool": "mba_offsite/1"}
        man_path = os.path.join(workdir, obj + ".manifest.json")
        try:
            with open(man_path, "w", encoding="utf-8") as handle:
                handle.write(common.json_dump(manifest))
        except OSError:
            raise StepError("manifest", "could not write the manifest")

        rc_env = {**base_env(), **{k: b2[k] for k in common.B2_ENV_KEYS if k != "MBA_BACKUP_BUCKET"}}
        bucket = b2["MBA_BACKUP_BUCKET"]
        for tier in tiers:  # recent first: there is an off-server copy before any extra copy is attempted
            remote_dir = f"b2:{bucket}/mba/{tier}"
            for local, remote_name in ((enc, obj), (man_path, obj + ".manifest.json")):
                code, _o, err = runner.run([rclone_bin, "copyto", "--no-check-bucket", local, f"{remote_dir}/{remote_name}"], env=rc_env, timeout=1800)
                if code != 0:
                    _note_error(state, err, secrets)
                    raise StepError("upload", f"rclone copyto exited with status {code}")
            code, listing, err = runner.run([rclone_bin, "lsjson", "--no-check-bucket", "--hash", "--no-modtime", "--files-only", remote_dir], env=rc_env, timeout=300)
            if code != 0:
                _note_error(state, err, secrets)
                raise StepError("verify", f"rclone lsjson exited with status {code}")
            try:
                entries = {e["Name"]: e for e in json.loads(listing)}
                entry = entries[obj]
                remote_sha1 = (entry.get("Hashes") or {}).get("sha1")
                same = entry["Size"] == manifest["size_encrypted"] and remote_sha1 == sha1 and (obj + ".manifest.json") in entries
            except (ValueError, KeyError, TypeError):
                same = False
            if not same:
                raise StepError("verify", f"the remote copy in {tier} does not match the local file")

        try:
            common.write_atomic(os.path.join(state, "last_offsite.json"), common.json_dump({"name": obj, "size_plain": size_plain, "sha256": sha256, "epoch": int(now), "tiers": tiers}))
            if "daily" in tiers:
                common.write_atomic(os.path.join(state, "last_daily_date"), time.strftime("%Y-%m-%d", time.gmtime(now)) + "\n")
                common.write_atomic(os.path.join(state, "last_daily_success"), f"{int(now)}\n")
            common.write_atomic(os.path.join(state, "last_offsite_success"), f"{int(now)}\n")
        except OSError:
            raise StepError("state", "could not write the success markers")
        common.log(f"offsite: sent {obj} ({manifest['size_encrypted']} bytes) to {', '.join(tiers)}")
        alert.ping_deadman()
        return EXIT_OK
    except StepError as exc:
        common.log(f"offsite: FAILED at step {exc.step}: {common.scrub(exc.detail, secrets)}")
        alert.alert_failure(exc.step, now)
        return EXIT_ALERTED
    except Exception as exc:  # noqa: BLE001 - anything unexpected is still a failed backup that must be reported
        common.log(f"offsite: FAILED (unexpected {type(exc).__name__})")
        alert.alert_failure("offsite", now)
        return EXIT_ALERTED
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _note_error(state: str, text: str, secrets: list[str]) -> None:
    """Keeps the last tool error, scrubbed of known secrets, in a private file for troubleshooting. Never printed."""
    try:
        common.write_atomic(os.path.join(state, "offsite_last_error.txt"), common.scrub(text, secrets)[:2000] + "\n")
    except OSError:
        pass


def check_config() -> int:
    try:
        common.parse_env_file(os.path.join(common.config_dir(), "b2.env"), common.B2_ENV_KEYS, required=common.B2_ENV_KEYS)
        common.read_recipient(os.path.join(common.config_dir(), "recipient.txt"))
        common.find_binary("age", "MBA_AGE_BIN")
        common.find_binary("rclone", "MBA_RCLONE_BIN")
        alert_cfg = alert.load_alert_config()
    except common.ConfigError as exc:
        common.log(f"config: NOT OK: {exc}")
        return EXIT_USAGE
    common.log("config: ok (b2.env, recipient.txt, age and rclone found)" + ("" if alert_cfg else "; alert.env is MISSING or unusable"))
    return EXIT_OK if alert_cfg else EXIT_USAGE


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args == ["--check-config"]:
        return check_config()
    accept = "--accept-shrink" in args
    args = [a for a in args if a != "--accept-shrink"]
    now = None
    if "--now" in args:
        i = args.index("--now")
        try:
            now = float(args[i + 1])
        except (IndexError, ValueError):
            sys.stderr.write("usage: --now needs an epoch number\n")
            return EXIT_USAGE
        del args[i:i + 2]
    if len(args) != 1:
        sys.stderr.write("usage: mba_offsite.py <path to one mba_bot-YYYYMMDD-HHMMSS.db> [--accept-shrink] [--now EPOCH]\n")
        return EXIT_USAGE
    return run(args[0], now=now, accept_shrink=accept)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
