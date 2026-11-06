#!/usr/bin/env python3
"""Read-only watchdog for the backups. Run hourly from cron:  7 * * * *  /usr/bin/python3 /opt/mba-bot/ops/backup/mba_watchdog.py

It reads the success markers the backup writes under /var/lib/mba-backup and alerts (Telegram, ops bot) when:
  * the last local backup or the last off-server upload is older than 8 hours (the job runs every 6 hours), or
  * no daily copy has been uploaded for 26 hours.
It exists because the trap inside backup_db.sh cannot fire when cron stops, the script file disappears or the machine skips a run.
It touches no backup file and no database. It stays silent when healthy, except for one "backups OK" message on Sundays after
09:00 UTC. An alert repeats at most every 6 hours per condition, and a short "recovered" message follows when it clears.
Messages are fixed text with ages in hours; no path, secret or file content.
"""
from __future__ import annotations

import json
import os
import sys
import time

import mba_alert as alert
import mba_common as common

STALE_6H = 8 * 3600
STALE_DAILY = 26 * 3600
REPEAT = 6 * 3600
OK_HOUR_UTC = 9


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}"


def evaluate(now: float, state: str) -> tuple[list[tuple[str, str]], dict]:
    """Returns (problems as (code, text), facts). Not armed (no marker yet) means no problems: nothing has run to be late."""
    local = common.read_epoch(os.path.join(state, "last_local_success"))
    offsite = common.read_epoch(os.path.join(state, "last_offsite_success"))
    daily = common.read_epoch(os.path.join(state, "last_daily_success"))
    facts = {"local": local, "offsite": offsite, "daily": daily}
    if local is None and offsite is None:
        return [], facts
    problems: list[tuple[str, str]] = []
    for code, label, value, limit in (
        ("LOCAL_STALE", "local backup", local, STALE_6H),
        ("OFFSITE_STALE", "off-server upload", offsite, STALE_6H),
        ("DAILY_STALE", "daily off-server copy", daily, STALE_DAILY),
    ):
        if value is None:
            problems.append((code, f"MBA backup watchdog: no {label} has ever succeeded on this server."))
        elif now - value > limit:
            problems.append((code, f"MBA backup watchdog: the last {label} was {_hours(now - value)} hours ago (limit {_hours(limit)})."))
    return problems, facts


def _load(path: str) -> dict:
    try:
        data = json.loads(common.read_text(path) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def run(now: float | None = None, state: str | None = None) -> int:
    now = time.time() if now is None else now
    state = state or common.state_dir()
    problems, facts = evaluate(now, state)
    memory_path = os.path.join(state, "watchdog_state.json")
    memory = _load(memory_path)
    alerted: dict = memory.get("alerted", {})
    changed = False
    current = {code for code, _ in problems}
    due = [(code, text) for code, text in problems if now - float(alerted.get(code, 0)) >= REPEAT]
    if due:  # one message for everything that is due, so three stale markers do not mean three messages
        if alert.send_telegram("\n".join(text for _code, text in due)):
            for code, _text in due:
                alerted[code] = now
            changed = True
            common.log("watchdog: alerted " + ", ".join(code for code, _ in due))
        else:
            common.log("watchdog: problems found but the alert could not be sent")
    cleared = [c for c in alerted if c not in current]
    if cleared:
        if alert.send_telegram("MBA backup watchdog: the backup problem has cleared (backups are current again)."):
            common.log("watchdog: recovered " + ", ".join(cleared))
        for code in cleared:
            del alerted[code]
        changed = True
    tm = time.gmtime(now)
    week = time.strftime("%G-W%V", tm)
    if not problems and facts["offsite"] is not None and tm.tm_wday == 6 and tm.tm_hour >= OK_HOUR_UTC and memory.get("ok_week") != week:
        text = f"MBA backups OK. Last off-server upload {_hours(now - facts['offsite'])} hours ago"
        if facts["daily"] is not None:
            text += f"; last daily copy {_hours(now - facts['daily'])} hours ago"
        if alert.send_telegram(text + "."):
            memory["ok_week"] = week
            changed = True
            common.log("watchdog: weekly OK sent")
    if changed:
        memory["alerted"] = alerted
        try:
            common.write_atomic(memory_path, common.json_dump(memory))
        except OSError:
            common.log("watchdog: could not save its own state")
    return 0


def main(argv: list[str]) -> int:
    now = None
    if len(argv) == 3 and argv[1] == "--now":
        try:
            now = float(argv[2])
        except ValueError:
            sys.stderr.write("usage: mba_watchdog.py [--now EPOCH]\n")
            return 2
    elif len(argv) != 1:
        sys.stderr.write("usage: mba_watchdog.py [--now EPOCH]\n")
        return 2
    return run(now)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
