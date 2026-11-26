"""Lock diagnosis, observation only (docs/safety/lock_watch.md).

Measures how long each WRITE transaction on the SQLite engine stays open, from its first write statement to its commit or rollback (a writer holds the database-wide write lock for that whole time; a connection closed with its write open is rolled back first, which ends the timing). When one lasts
longer than LOCK_WATCH_SECONDS (3 by default; 0 = off) it logs ONE warning line with the duration and the call site as FUNCTION NAMES of this package only (no statement text, no parameters, no ids, no values, no
file paths). At most LOCK_WATCH_MAX_LINES_PER_HOUR lines (20) per hour, so a bad hour cannot flood the log.

It never changes a transaction, a statement, a timeout or a lock: it only reads the clock and logs. Every hook body is wrapped so that it cannot raise into the database layer.

It also keeps three counters (write transactions, total and longest hold) that `startup_probe` uses to say once, at start-up, how long the start-up steps held the database."""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_KEY = "_mba_write_started"
_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLACE")
MAX_FRAMES = 6

_lock = threading.Lock()
_emitted: deque[float] = deque()
_stats = {"transactions": 0, "total": 0.0, "longest": 0.0}


def _call_site() -> str:
    """Function names of the mba_bot code that is running, innermost first, at most MAX_FRAMES, joined by '<-'. Names only: nothing from the frames' data is read."""
    names: list[str] = []
    try:
        frame = sys._getframe(2)
        while frame is not None and len(names) < MAX_FRAMES:
            code = frame.f_code
            if "mba_bot" in code.co_filename and not code.co_filename.endswith("lock_watch.py"):
                names.append(code.co_name)
            frame = frame.f_back
    except Exception:  # noqa: BLE001
        pass
    return "<-".join(names) or "unknown"


def _allowed(now: float, max_per_hour: int) -> bool:
    with _lock:
        while _emitted and now - _emitted[0] > 3600.0:
            _emitted.popleft()
        if len(_emitted) >= max_per_hour:
            return False
        _emitted.append(now)
        return True


def _finish(info: dict, how: str, threshold: float, max_per_hour: int) -> None:
    started = info.pop(_KEY, None)
    if started is None:
        return
    began, site = started
    now = time.monotonic()
    held = now - began
    with _lock:
        _stats["transactions"] += 1
        _stats["total"] += held
        if held > _stats["longest"]:
            _stats["longest"] = held
    if held > threshold and _allowed(now, max_per_hour):
        logger.warning("write transaction held the database for %.1fs (ended by %s; started in %s)", held, how, site)


def install(engine: Engine, *, threshold_seconds: float = 3.0, max_lines_per_hour: int = 20) -> bool:
    """Attach the hooks to a SQLite engine. Returns whether they were attached (False for another database or when threshold_seconds <= 0). Safe to call once per engine."""
    try:
        if threshold_seconds <= 0 or engine.dialect.name != "sqlite":
            return False

        @event.listens_for(engine, "before_cursor_execute")
        def _on_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            try:
                info = conn.info
                if _KEY not in info and statement.lstrip()[:7].upper().startswith(_WRITE_VERBS):
                    info[_KEY] = (time.monotonic(), _call_site())
            except Exception:  # noqa: BLE001 - a diagnostic must never touch the database layer
                pass

        @event.listens_for(engine, "commit")
        def _on_commit(conn):  # noqa: ANN001
            try:
                _finish(conn.info, "commit", threshold_seconds, max_lines_per_hour)
            except Exception:  # noqa: BLE001
                pass

        @event.listens_for(engine, "rollback")
        def _on_rollback(conn):  # noqa: ANN001
            try:
                _finish(conn.info, "rollback", threshold_seconds, max_lines_per_hour)
            except Exception:  # noqa: BLE001
                pass

        return True
    except Exception:  # noqa: BLE001
        return False


def snapshot() -> tuple[int, float, float]:
    with _lock:
        return _stats["transactions"], _stats["total"], _stats["longest"]


@contextmanager
def startup_probe(name: str) -> Iterator[None]:
    """Time one start-up step and say, in one INFO line, how long it took and how long its write transactions held the database (count, total, longest). Never raises and never changes what the step does; an
    exception from the step itself passes through untouched (the line is then not written)."""
    try:
        t0, before = time.monotonic(), snapshot()
    except Exception:  # noqa: BLE001
        t0, before = None, None
    yield
    try:
        if t0 is None or before is None:
            return
        after = snapshot()
        logger.info("start-up %s: took %.2fs; write transactions %d, held the database %.2fs in total (longest single hold %.2fs)", name, time.monotonic() - t0, after[0] - before[0], after[1] - before[1], after[2])
    except Exception:  # noqa: BLE001
        pass


def reset_for_tests() -> None:
    with _lock:
        _emitted.clear()
        _stats.update(transactions=0, total=0.0, longest=0.0)
