"""The two optional process roles (docs/safety/process_split.md). `all` is the default and is today's single process, untouched. `api` and `worker` split it so that API and billing code can be released without
restarting the trading worker.

Guards here, each failing CLOSED (the process refuses to start rather than run in a way nobody planned):
  * SQLite must be in WAL mode (SQLITE_WAL=1): two processes write the same file, and a rollback journal would make them block each other;
  * a process refuses a role that is not its own (the worker refuses PROCESS_ROLE=api, the API refuses PROCESS_ROLE=worker): one shared environment file with the wrong value cannot silently start two schedulers
    or a trading loop inside the HTTP process;
  * the scheduler lock is taken by `all` and by the worker, so two schedulers cannot run; the API has its own lock file.
`preload` imports every trading module at start-up, so a code release on disk while the worker runs can never be picked up half way by a late import (the worker keeps the code it started with until it is restarted)."""
from __future__ import annotations

import importlib
import pkgutil
import sqlite3
import sys

ROLE_ALL, ROLE_API, ROLE_WORKER = "all", "api", "worker"
SKIP_PACKAGES = ("mba_bot.api", "mba_bot.research")


class RoleError(RuntimeError):
    pass


def require_wal_setting(settings) -> None:
    """Split roles on SQLite need SQLITE_WAL=1. Other databases are not judged here."""
    if str(settings.database_url).startswith("sqlite") and not settings.sqlite_wal:
        raise RoleError("PROCESS_ROLE api or worker needs SQLITE_WAL=1: two processes write the same SQLite file. Enable WAL first (docs/security/wal_runbook.md).")


def journal_mode(database_url: str) -> str | None:
    """The journal mode stored in the SQLite file ('wal', 'delete', ...), or None for another database or an unreadable file. Read-only."""
    if not str(database_url).startswith("sqlite") or ":memory:" in str(database_url):
        return None
    path = str(database_url).split("///", 1)[-1].split("?", 1)[0]
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=8)
        try:
            return str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        finally:
            con.close()
    except sqlite3.Error:
        return None


def require_wal_on_disk(settings) -> None:
    mode = journal_mode(settings.database_url)
    if str(settings.database_url).startswith("sqlite") and ":memory:" not in str(settings.database_url) and mode != "wal":
        raise RoleError(f"the database is in {mode or 'an unreadable'} journal mode, not WAL: do not run the split roles until it is (docs/security/wal_runbook.md).")


def refuse_other_role(settings, own: str) -> None:
    if settings.process_role not in (ROLE_ALL, own):
        raise RoleError(f"PROCESS_ROLE={settings.process_role} is set, but this process is the {own}. Check that the shared environment file does not set PROCESS_ROLE (each unit sets its own).")


def preload() -> list[str]:
    """Import every module of the package except the web layer and the research tools. Returns the modules that failed to import (an empty list when all is well)."""
    import mba_bot

    failed = []
    for info in pkgutil.walk_packages(mba_bot.__path__, "mba_bot."):
        if info.name.startswith(SKIP_PACKAGES) or info.name.endswith(".__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001 - a module that cannot import is reported by name and class only
            failed.append(f"{info.name} ({type(exc).__name__})")
    return failed


def loaded_modules() -> set[str]:
    return {m for m in sys.modules if m.startswith("mba_bot")}
