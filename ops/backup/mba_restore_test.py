#!/usr/bin/env python3
"""Restore drill on a SCRATCH copy. Run it on your own computer, never as a way to overwrite the live database.

    python3 mba_restore_test.py --key /path/to/mba-backup-private.key [--tier daily]
    python3 mba_restore_test.py --key KEYFILE --from-dir /path/to/a/local/mirror   (rehearsal against a folder laid out like the bucket)

From Backblaze it asks, at hidden prompts, for the bucket name and the RESTORE application key (read and list); nothing is read
from the command line or saved. It then: finds the newest object in the tier, downloads it and its manifest, checks the SHA-256
against the manifest, decrypts with your private key (age), decompresses into a scratch folder, runs integrity_check and
foreign_key_check, prints table counts and the alembic version, and deletes the scratch folder. It prints counts and names only,
never row contents. Needs age, rclone (not for --from-dir) and Python 3.9 or newer.
"""
from __future__ import annotations

import argparse
import getpass
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse

COUNT_TABLES = ("customers", "trading_accounts", "trades", "kill_switch_events")


def sh(argv, env=None):
    done = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"step failed: {os.path.basename(argv[0])} exited with status {done.returncode}")
    return done.stdout


def fetch_remote(bucket: str, tier: str, scratch: str) -> tuple[str, str]:
    env = {**os.environ, "RCLONE_CONFIG_B2_TYPE": "b2", "RCLONE_CONFIG_B2_ACCOUNT": getpass.getpass("restore key ID (hidden): "),
           "RCLONE_CONFIG_B2_KEY": getpass.getpass("restore application key (hidden): ")}
    base = f"b2:{bucket}/mba/{tier}"
    names = [e["Name"] for e in json.loads(sh(["rclone", "lsjson", "--no-check-bucket", "--files-only", base], env))]
    objs = sorted(n for n in names if n.endswith(".gz.age"))
    if not objs:
        raise SystemExit(f"no backups found in {tier}")
    newest = objs[-1]
    for name in (newest, newest + ".manifest.json"):
        sh(["rclone", "copyto", "--no-check-bucket", f"{base}/{name}", os.path.join(scratch, name)], env)
    return os.path.join(scratch, newest), os.path.join(scratch, newest + ".manifest.json")


def fetch_local(root: str, tier: str, scratch: str) -> tuple[str, str]:
    folder = os.path.join(root, "mba", tier)
    objs = sorted(n for n in os.listdir(folder) if n.endswith(".gz.age"))
    if not objs:
        raise SystemExit(f"no backups found in {tier}")
    newest = objs[-1]
    for name in (newest, newest + ".manifest.json"):
        shutil.copy2(os.path.join(folder, name), os.path.join(scratch, name))
    return os.path.join(scratch, newest), os.path.join(scratch, newest + ".manifest.json")


def verify_and_open(enc: str, manifest_path: str, key: str, scratch: str) -> str:
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    digest = hashlib.sha256(open(enc, "rb").read()).hexdigest()
    if digest != manifest["sha256_encrypted"]:
        raise SystemExit("CHECKSUM MISMATCH: the downloaded file does not match its manifest")
    print(f"object {manifest['object']} created {manifest['created_utc']}; encrypted {manifest['size_encrypted']} bytes; sha256 matches the manifest")
    gz = os.path.join(scratch, "restored.db.gz")
    sh(["age", "-d", "-i", key, "-o", gz, enc])
    db = os.path.join(scratch, "restored.db")
    with gzip.open(gz, "rb") as src, open(db, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    os.unlink(gz)
    return db


def inspect(db: str) -> dict:
    con = sqlite3.connect(f"file:{urllib.parse.quote(db)}?mode=ro&immutable=1", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchall()
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNT_TABLES if t in tables}
        version = con.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone() if "alembic_version" in tables else None
        return {"integrity_ok": integrity == [("ok",)], "foreign_key_problems": len(fk), "counts": counts, "alembic_version": version[0] if version else None}
    finally:
        con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, help="path of your age PRIVATE key file")
    ap.add_argument("--tier", default="daily", choices=("recent", "daily", "weekly", "monthly"))
    ap.add_argument("--from-dir", help="a folder laid out like the bucket (mba/<tier>/...), for a rehearsal without Backblaze")
    args = ap.parse_args(argv)
    scratch = tempfile.mkdtemp(prefix="mba-restore-")
    try:
        if args.from_dir:
            enc, man = fetch_local(args.from_dir, args.tier, scratch)
        else:
            enc, man = fetch_remote(getpass.getpass("bucket name: "), args.tier, scratch)
        facts = inspect(verify_and_open(enc, man, args.key, scratch))
        print("integrity_check:", "ok" if facts["integrity_ok"] else "FAILED")
        print("foreign_key_check problems:", facts["foreign_key_problems"])
        print("alembic version:", facts["alembic_version"])
        for table, n in facts["counts"].items():
            print(f"rows in {table}: {n}")
        ok = facts["integrity_ok"] and facts["foreign_key_problems"] == 0
        print("RESTORE TEST", "PASSED" if ok else "FAILED")
        return 0 if ok else 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        print("scratch folder removed")


if __name__ == "__main__":
    sys.exit(main())
