"""Operator command for the kill switch. Run on the server as the app's user, from the app directory:

    python -m mba_bot.killswitch status
    python -m mba_bot.killswitch engage --reason "why"
    python -m mba_bot.killswitch release --reason "why" --confirm RELEASE

Engaging stops NEW live entries on every account (open positions are not touched). Releasing needs a reason and the typed
word RELEASE. If the database write fails, the command says so (exit status 1) and prints the break-glass command; it
never reports success for a write that did not happen. Nothing here prints secrets, and it never creates tables.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from . import kill_switch


_WRITE_TIMEOUT = 8.0  # seconds SQLite waits for a lock on each attempt
_WRITE_RETRY_SECONDS = 8.0  # how long a write keeps retrying before the command reports failure


def _engine(settings):
    from sqlalchemy import create_engine

    url = settings.database_url
    args = {"timeout": _WRITE_TIMEOUT} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=args)


def _actor() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "unknown"
    return ("cli:" + user)[:60]


def _out(*lines: str) -> None:
    print("\n".join(lines))


def _file_note(settings) -> list[str]:
    state = kill_switch._read_file(settings.kill_switch_file)
    if state == "present":
        return [f"  break-glass file: PRESENT ({settings.kill_switch_file}) - it keeps the switch engaged until you remove it"]
    if state == "error":
        return [f"  break-glass file: UNREADABLE ({settings.kill_switch_file}) - this counts as engaged"]
    return [f"  break-glass file: absent ({settings.kill_switch_file})"]


def cmd_status(engine, settings) -> int:
    exist = kill_switch.tables_exist(engine)
    if exist is None:
        _out("could not read the database (is it locked?). Try again in a moment. To engage by file instead: touch " + settings.kill_switch_file)
        return 1
    if not exist:
        _out("kill switch tables do not exist in this database yet. Start the app once, or apply the migration.",
             "Until then the app treats the switch as engaged. To engage by file: touch " + settings.kill_switch_file)
        return 1
    row = kill_switch.read_row(engine)
    decision = kill_switch.check(engine, settings)
    lines = [f"kill switch: {'ENGAGED' if decision.engaged else 'RELEASED'}  (effective path={decision.path}, read attempts={decision.attempts})"]
    if row.state in ("engaged", "released"):
        who = f" by {row.by}" if row.by else ""
        when = f" at {row.since}" if row.since else ""
        why = f": {row.reason}" if row.reason else ""
        lines.append(f"  database flag   : {row.state}{when}{who}{why}")
    else:
        lines.append(f"  database flag   : {row.state.upper()} (counts as engaged)")
    lines += _file_note(settings)
    events = kill_switch.recent_events(engine, 5)
    lines.append("  recent events   : " + ("none" if not events else ""))
    for at, action, actor, reason in events:
        lines.append(f"    {at}  {action:<7} {actor}  {reason or ''}")
    _out(*lines)
    return 0


def cmd_engage(engine, settings, reason: str | None) -> int:
    cleaned = kill_switch.clean_reason(reason)
    if cleaned is None:
        _out("refused: engage needs --reason \"text\" (it is written to the audit log).")
        return 2
    if kill_switch.tables_exist(engine) is False:
        _out("FAILED: the kill switch tables do not exist in this database yet, so nothing was written.",
             f"Engage by file instead, right now:  touch {settings.kill_switch_file}",
             f"Remove it later with:  rm {settings.kill_switch_file}")
        return 1
    result = kill_switch.set_state(engine, engaged=True, actor=_actor(), reason=cleaned, retry_seconds=_WRITE_RETRY_SECONDS)
    if not result.ok:
        _out(f"FAILED: the database write did not succeed ({result.error}, {result.attempts} attempts). The switch is NOT engaged by the database.",
             f"Engage by file instead, right now:  touch {settings.kill_switch_file}",
             f"Remove it later with:  rm {settings.kill_switch_file}")
        return 1
    _out(f"KILL SWITCH ENGAGED in the database. Reason: {cleaned}",
         "New live entries stop at the next scheduler read (within about 5 minutes). Open positions stay managed and protected.",
         "To release it:  " + kill_switch.release_command(),
         "To check it:    python -m mba_bot.killswitch status")
    return 0


def cmd_release(engine, settings, reason: str | None, confirm: str | None) -> int:
    cleaned = kill_switch.clean_reason(reason)
    if cleaned is None or confirm != "RELEASE":
        _out("refused: release needs --reason \"text\" and --confirm RELEASE.", "Nothing was changed.")
        return 2
    if kill_switch.tables_exist(engine) is False:
        _out("FAILED: the kill switch tables do not exist in this database yet, so nothing was written.")
        return 1
    result = kill_switch.set_state(engine, engaged=False, actor=_actor(), reason=cleaned, retry_seconds=_WRITE_RETRY_SECONDS)
    if not result.ok:
        _out(f"FAILED: the database write did not succeed ({result.error}, {result.attempts} attempts). The switch is unchanged.")
        return 1
    lines = [f"Kill switch released in the database. Reason: {cleaned}"]
    if kill_switch._read_file(settings.kill_switch_file) != "absent":
        lines += [f"WARNING: the break-glass file is still there and still engages the switch. Remove it:  rm {settings.kill_switch_file}"]
    else:
        lines += ["New live entries resume at the next scheduler read."]
    _out(*lines)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mba_bot.killswitch", description="Operator kill switch for new live entries.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show the state (read only)")
    e = sub.add_parser("engage", help="stop new live entries on every account")
    e.add_argument("--reason", required=False, help="why (written to the audit log)")
    r = sub.add_parser("release", help="allow new live entries again")
    r.add_argument("--reason", required=False)
    r.add_argument("--confirm", required=False, help="type RELEASE")
    args = parser.parse_args(argv)

    from .config import load_settings

    settings = load_settings()
    engine = _engine(settings)
    try:
        if args.command == "status":
            return cmd_status(engine, settings)
        if args.command == "engage":
            return cmd_engage(engine, settings, args.reason)
        return cmd_release(engine, settings, args.reason, args.confirm)
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
