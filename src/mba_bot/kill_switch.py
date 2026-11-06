"""Operator kill switch: stops NEW live entries on every account. Design and runbook: docs/safety/kill_switch.md.

What it does and does not do
  * Engaged: no new LIVE entry is opened on any account. Paper entries continue. Open positions are NOT closed and keep
    being managed (stop monitoring, protection, reconciliation, strategy exits, emergency exits). Protection is never
    gated by this module.
  * Two sources, both read fresh on every check (no cache): the singleton database row, and a break-glass file. Engaged
    if EITHER says engaged.
  * Fail closed: if the state cannot be read, the row is missing or malformed, or the file check fails with anything
    other than "not found", the answer is ENGAGED. A failed read is retried for a few seconds first
    (`kill_switch_read_retry_seconds`) because a locked database is usually momentary. Every Decision says which path
    was taken, and so does the log line.
  * Default after the migration or on a first run is RELEASED, so shipping this changes nothing until the owner engages
    it.

The check reads through its own short connection (not the caller's transaction), so a failed read never disturbs the
caller's session and it only ever sees committed data.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Integer, func, insert, select, type_coerce, update

from . import protection_guard
from .models import KillSwitch, KillSwitchEvent
from .venue import EntryForbidden

logger = logging.getLogger(__name__)

# Which path an answer came from. The first group is "may trade", the rest are "engaged".
PATH_RELEASED = "released"
PATH_DB = "engaged_db"
PATH_FILE = "engaged_file"
PATH_DB_AND_FILE = "engaged_db_and_file"
PATH_FC_DB = "fail_closed_db_unreadable"
PATH_FC_ROW_MISSING = "fail_closed_row_missing"
PATH_FC_ROW_MALFORMED = "fail_closed_row_malformed"
PATH_FC_FILE = "fail_closed_file_error"

# Fixed customer wording: no reason text, no secrets.
CUSTOMER_MESSAGE = "MBA Bot did not open a trade: new trades are paused by the operator. Any open position stays protected."

REASON_MAX = 200
def command(subcommand: str) -> str:
    """The exact shell command that runs this tool from where it is running now. The source folder is on PYTHONPATH and the
    interpreter is the one in use, because on the server the package is not installed in the virtual environment: a bare
    `python -m mba_bot.killswitch` fails there with "No module named mba_bot". Run it from the same directory (the database
    path in DATABASE_URL is relative to it)."""
    src = str(Path(__file__).resolve().parent.parent)
    return f"PYTHONPATH={shlex.quote(src)} {shlex.quote(sys.executable)} -m mba_bot.killswitch {subcommand}"


def release_command() -> str:
    """The exact command that releases the switch (printed by `engage`)."""
    return command('release --reason "<why>" --confirm RELEASE')


def status_command() -> str:
    return command("status")


@dataclass(frozen=True)
class Decision:
    engaged: bool
    path: str
    reason: str | None = None
    since: object = None
    by: str | None = None
    attempts: int = 1

    @property
    def fail_closed(self) -> bool:
        return self.path.startswith("fail_closed")


class EntryPaused(EntryForbidden):
    """A new live entry was attempted while the kill switch is engaged. Raised by lifecycle.open_live_trade as the last
    line of defence; the trader normally stops the entry earlier."""

    def __init__(self, decision: Decision):
        super().__init__(f"new live entries are paused (kill switch, path={decision.path})")
        self.decision = decision


# ---------------------------------------------------------------------------------------------- reading


@dataclass(frozen=True)
class _DbRead:
    state: str  # released | engaged | missing | malformed | error
    reason: str | None = None
    since: object = None
    by: str | None = None


def _engine_of(source):
    getter = getattr(source, "get_bind", None)
    return getter() if callable(getter) else source


def _read_db(engine) -> _DbRead:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(
                    type_coerce(KillSwitch.engaged, Integer), KillSwitch.reason, KillSwitch.changed_at, KillSwitch.changed_by
                ).where(KillSwitch.id == 1)
            ).one_or_none()
    except Exception:  # noqa: BLE001 - a locked database, a missing table, a bad value: all "could not read"
        return _DbRead("error")
    if row is None:
        return _DbRead("missing")
    raw, reason, since, by = row
    if raw is None or isinstance(raw, str) or raw not in (0, 1):
        return _DbRead("malformed")
    return _DbRead("engaged" if raw else "released", reason, since, by)


def _read_file(path: str | None) -> str:
    """present | absent | error. Only FileNotFoundError means absent."""
    if not path:
        return "absent"
    try:
        os.stat(path)
        return "present"
    except FileNotFoundError:
        return "absent"
    except Exception:  # noqa: BLE001 - permission error, I/O error, a parent that is a file, ...
        return "error"


def _evaluate(engine, path: str | None, attempts: int):
    """Returns a Decision when the answer is certain, else the string "db" or "file" naming what could not be read."""
    db = _read_db(engine)
    f = _read_file(path)
    if db.state == "engaged":
        return Decision(True, PATH_DB_AND_FILE if f == "present" else PATH_DB, db.reason, db.since, db.by, attempts)
    if f == "present":
        reason = db.reason if db.state == "released" else None
        return Decision(True, PATH_FILE, reason, db.since, db.by, attempts)
    if db.state == "missing":
        return Decision(True, PATH_FC_ROW_MISSING, None, None, None, attempts)
    if db.state == "malformed":
        return Decision(True, PATH_FC_ROW_MALFORMED, None, None, None, attempts)
    if db.state == "error":
        return "db"
    if f == "error":
        return "file"
    return Decision(False, PATH_RELEASED, db.reason, db.since, db.by, attempts)


def _exhausted(what: str, attempts: int) -> Decision:
    return Decision(True, PATH_FC_DB if what == "db" else PATH_FC_FILE, None, None, None, attempts)


def _cfg(settings) -> tuple[str | None, float, float]:
    return (
        getattr(settings, "kill_switch_file", None),
        max(0.0, float(getattr(settings, "kill_switch_read_retry_seconds", 3.0))),
        max(0.01, float(getattr(settings, "kill_switch_read_retry_interval_seconds", 0.5))),
    )


def check(source, settings, *, sleep=time.sleep, monotonic=time.monotonic) -> Decision:
    """Synchronous check (the CLI, tests). `source` is a Session or an Engine. Never raises."""
    path, retry_s, interval = _cfg(settings)
    try:
        engine = _engine_of(source)
        deadline = monotonic() + retry_s
        attempts = 0
        while True:
            attempts += 1
            outcome = _evaluate(engine, path, attempts)
            if isinstance(outcome, Decision):
                return outcome
            if monotonic() + interval > deadline:
                return _exhausted(outcome, attempts)
            sleep(interval)
    except Exception:  # noqa: BLE001 - never let the safety check itself raise into the trading loop
        return Decision(True, PATH_FC_DB, None, None, None, 1)


async def acheck(source, settings) -> Decision:
    """Async check for the scheduler: same rules, but waits with asyncio.sleep between attempts. Never raises."""
    path, retry_s, interval = _cfg(settings)
    try:
        engine = _engine_of(source)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + retry_s
        attempts = 0
        while True:
            attempts += 1
            outcome = _evaluate(engine, path, attempts)
            if isinstance(outcome, Decision):
                return outcome
            if loop.time() + interval > deadline:
                return _exhausted(outcome, attempts)
            await asyncio.sleep(interval)
    except Exception:  # noqa: BLE001
        return Decision(True, PATH_FC_DB, None, None, None, 1)


def log_decision(decision: Decision, *, scope: str, now: datetime, repeat_hours: float) -> None:
    """One journal line saying which path was taken, at most once per `repeat_hours` per scope and path. Used for an
    engaged or fail-closed answer, and for a released answer that needed more than one attempt."""
    if not protection_guard.should_alert(scope, "-", "kill_switch:" + decision.path, now=now, repeat_hours=repeat_hours):
        return
    if decision.fail_closed:
        logger.warning(
            "account %s: kill switch state could not be read cleanly (path=%s after %d attempt(s)): treating it as "
            "ENGAGED, new live entries are paused; open positions stay managed",
            scope, decision.path, decision.attempts,
        )
    elif decision.engaged:
        logger.warning(
            "account %s: kill switch ENGAGED (path=%s, since=%s, by=%s, reason=%s, attempts=%d): new live entries are "
            "paused; open positions stay managed",
            scope, decision.path, decision.since, decision.by, decision.reason, decision.attempts,
        )
    else:
        logger.warning("account %s: kill switch read needed %d attempts (path=%s)", scope, decision.attempts, decision.path)


# ---------------------------------------------------------------------------------------------- writing (CLI)


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    error: str | None = None
    attempts: int = 1


def clean_reason(text: str | None) -> str | None:
    """Collapses whitespace and control characters, trims, caps at REASON_MAX. None if nothing is left."""
    if text is None:
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f\s]+", " ", text).strip()[:REASON_MAX].strip()
    return cleaned or None


def set_state(
    engine, *, engaged: bool, actor: str, reason: str, retry_seconds: float = 8.0, interval: float = 0.5,
    now: datetime | None = None, sleep=time.sleep, monotonic=time.monotonic,
) -> WriteResult:
    """Writes the singleton row and ONE audit event in one transaction, retrying a locked database for
    `retry_seconds`. Never raises; the caller must look at `ok` (a failure must never be reported as success)."""
    now = now or datetime.now(timezone.utc)
    deadline = monotonic() + max(0.0, retry_seconds)
    attempts = 0
    while True:
        attempts += 1
        try:
            with engine.begin() as conn:
                prev = conn.execute(select(type_coerce(KillSwitch.engaged, Integer)).where(KillSwitch.id == 1)).scalar_one_or_none()
                values = dict(engaged=engaged, reason=reason, changed_at=now, changed_by=actor)
                if prev is None:
                    conn.execute(insert(KillSwitch).values(id=1, **values))
                else:
                    conn.execute(update(KillSwitch).where(KillSwitch.id == 1).values(**values))
                conn.execute(
                    insert(KillSwitchEvent).values(
                        id=str(uuid.uuid4()), at=now, action="engage" if engaged else "release", actor=actor,
                        reason=reason, prev_engaged=None if prev is None else bool(prev),
                    )
                )
            return WriteResult(True, None, attempts)
        except Exception as exc:  # noqa: BLE001
            error = type(exc).__name__
            if monotonic() + interval > deadline:
                return WriteResult(False, error, attempts)
            sleep(interval)


def read_row(engine) -> _DbRead:
    return _read_db(engine)


def recent_events(engine, limit: int = 5) -> list[tuple]:
    """(at, action, actor, reason) newest first; an empty list if the table cannot be read."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(KillSwitchEvent.at, KillSwitchEvent.action, KillSwitchEvent.actor, KillSwitchEvent.reason)
                .order_by(KillSwitchEvent.at.desc()).limit(limit)
            ).all()
        return [tuple(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def tables_exist(engine) -> bool | None:
    """True / False, or None when the database could not be read at all (for example it is locked). None must not be
    reported as "the tables are missing"."""
    from sqlalchemy import inspect

    try:
        names = set(inspect(engine).get_table_names())
    except Exception:  # noqa: BLE001
        return None
    return {"kill_switch", "kill_switch_events"} <= names


def ensure_state_row(engine) -> str:
    """First-run seed: if there is no row AND no audit event has ever been written, insert a RELEASED row and a 'seed'
    event. A missing row with events present is an anomaly and is left alone (the check then fails closed).
    Returns "present" | "seeded" | "anomaly" | "error". Never raises (called from init_db)."""
    try:
        with engine.begin() as conn:
            if conn.execute(select(func.count()).select_from(KillSwitch)).scalar_one():
                return "present"
            if conn.execute(select(func.count()).select_from(KillSwitchEvent)).scalar_one():
                logger.error("kill switch row is missing but audit events exist: leaving it missing, trading stays paused until it is repaired")
                return "anomaly"
            now = datetime.now(timezone.utc)
            conn.execute(insert(KillSwitch).values(id=1, engaged=False, reason=None, changed_at=now, changed_by="seed"))
            conn.execute(
                insert(KillSwitchEvent).values(
                    id=str(uuid.uuid4()), at=now, action="seed", actor="seed", reason="first run", prev_engaged=None
                )
            )
        logger.info("kill switch: created the state row (released)")
        return "seeded"
    except Exception as exc:  # noqa: BLE001
        logger.warning("kill switch: could not create the state row (%s); new live entries stay paused until it exists", type(exc).__name__)
        return "error"
