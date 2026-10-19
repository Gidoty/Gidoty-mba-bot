"""Single-instance guard for the scheduler process.

Two scheduler processes against one database would each run their own
cycle for the same account (e.g. an overlapping systemd restart, or
someone starting `python -m mba_bot.main` by hand next to the service).
The trade state machine is safe against that - claims are atomic, client
order ids are deterministic, the database allows one non-terminal trade per
account - but there is no reason to rely on the safety nets when the
process simply refuses to start twice.

An exclusive, non-blocking `flock` on a lock file is held for the life of
the process and released by the OS if it dies, so a crash never leaves a
stale lock behind.
"""
from __future__ import annotations

import fcntl
import os


class AlreadyRunning(RuntimeError):
    pass


def acquire_single_instance_lock(path: str):
    """Returns the open file object; the caller must keep a reference to it
    for as long as the process runs. Raises AlreadyRunning if another
    process holds the lock."""
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AlreadyRunning(f"another scheduler instance already holds {path}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle
