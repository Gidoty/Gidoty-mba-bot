#!/usr/bin/env python3
"""Read-only watchdog for the trading rounds. Run every 5 minutes from cron:  */5 * * * *  /usr/bin/python3 /opt/mba-bot/ops/health/mba_round_watchdog.py

It exists because nothing INSIDE the bot can say "the bot has stopped". It reuses the backup watchdog's alert sender, markers directory and message rules (ops/backup/mba_alert.py and mba_common.py: the ops Telegram bot,
fixed text, no secret in a message, best effort), and its pattern: alert when a problem starts, repeat at most every hour, one short message when it clears.

It reads the database READ-ONLY (it never creates or writes a table) and asks systemd whether the service is active. It alerts when:
  * WORKER_DOWN: the service is not active (the message also says how many live trades are open: their stop-losses stay on the exchange, but nothing is managing them);
  * ROUNDS_STALE: the service is active but no account has been scanned for MBA_ROUND_STALE_SECONDS (default 1200, four rounds): the rounds are not completing;
  * DB_UNREADABLE: the database cannot be read.
Not armed (silent) when no account is active or no account has ever been scanned: there is no round to be late. It places no order and changes nothing. Messages are fixed text with counts and minutes only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backup"))
import mba_alert as alert  # noqa: E402
import mba_common as common  # noqa: E402

STALE_SECONDS = int(os.environ.get("MBA_ROUND_STALE_SECONDS", "1200"))
REPEAT = 3600
TERMINAL = ("closed", "void")


def _epoch(value) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def read_facts(db_path: str) -> dict | None:
    """{"active": accounts switched on, "newest": epoch of the newest scan or None, "live_open": live trades not yet closed} or None when the database cannot be read. Opens the file read-only; a missing file is not created."""
    if not os.path.exists(db_path):
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=8)
        try:
            active = con.execute("select count(*) from trading_accounts where is_active = 1").fetchone()[0]
            newest = con.execute("select max(last_scanned_at) from trading_accounts where is_active = 1").fetchone()[0]
            marks = ",".join("?" for _ in TERMINAL)
            live_open = con.execute(f"select count(*) from trades where is_paper = 0 and is_demo = 0 and status not in ({marks})", TERMINAL).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return {"active": int(active), "newest": _epoch(newest), "live_open": int(live_open)}


def service_active(service: str) -> bool | None:
    """True or False from systemd, None when systemctl cannot be run (then only the rounds are judged)."""
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", service], timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def evaluate(now: float, facts: dict | None, active: bool | None, stale_seconds: int = STALE_SECONDS) -> list[tuple[str, str]]:
    if facts is None:
        return [("DB_UNREADABLE", "MBA health watchdog: the database cannot be read from here. Check the server.")]
    if facts["active"] == 0 or facts["newest"] is None:
        return []  # not armed: no active account has ever been scanned, so there is no round to be late
    trades = facts["live_open"]
    trade_text = f" {trades} live trade(s) are open: their stop-losses stay on the exchange but nothing is managing them." if trades else " No live trade is open."
    if active is False:
        return [("WORKER_DOWN", "MBA health watchdog: the bot service is NOT running." + trade_text)]
    age = now - facts["newest"]
    if age > stale_seconds:
        return [("ROUNDS_STALE", f"MBA health watchdog: the bot service is running but no account has been checked for {round(age / 60)} minutes (limit {round(stale_seconds / 60)})." + trade_text)]
    return []


def _load(path: str) -> dict:
    try:
        data = json.loads(common.read_text(path) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def run(now: float | None = None, db_path: str | None = None, service: str | None = None, state: str | None = None) -> int:
    now = time.time() if now is None else now
    db_path = db_path or os.environ.get("MBA_DB", "/opt/mba-bot/mba_bot.db")
    service = service or os.environ.get("MBA_SERVICE", "mba-bot")
    state = state or common.state_dir()
    problems = evaluate(now, read_facts(db_path), service_active(service))
    path = os.path.join(state, "round_watchdog_state.json")
    memory = _load(path)
    alerted: dict = memory.get("alerted", {})
    changed = False
    current = {code for code, _ in problems}
    due = [(code, text) for code, text in problems if now - float(alerted.get(code, 0)) >= REPEAT]
    if due:
        if alert.send_telegram("\n".join(text for _c, text in due)):
            for code, _t in due:
                alerted[code] = now
            changed = True
            common.log("round watchdog: alerted " + ", ".join(c for c, _ in due))
        else:
            common.log("round watchdog: problems found but the alert could not be sent")
    cleared = [c for c in alerted if c not in current]
    if cleared:
        if alert.send_telegram("MBA health watchdog: the bot is checking accounts again."):
            common.log("round watchdog: recovered " + ", ".join(cleared))
        for code in cleared:
            del alerted[code]
        changed = True
    if changed:
        memory["alerted"] = alerted
        try:
            common.write_atomic(path, common.json_dump(memory))
        except OSError:
            common.log("round watchdog: could not write its state file")
    return 0


if __name__ == "__main__":
    sys.exit(run())
