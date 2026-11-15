"""Operator command for strategy pauses written by the strategy guards (docs/safety/strategy_guards.md). Run on the server as the
app's user, FROM THE APP DIRECTORY (the database path in DATABASE_URL is relative to it):

    cd /opt/mba-bot
    PYTHONPATH=/opt/mba-bot/src .venv/bin/python -m mba_bot.strategy_pause status
    PYTHONPATH=/opt/mba-bot/src .venv/bin/python -m mba_bot.strategy_pause release --account <id or first 8 characters> \\
        --note "why" --confirm RELEASE

`status` only reads. `release` ends the active pauses of one account (both modes unless --mode paper|live is given) and needs a note
and the typed word RELEASE. After a release only trades closed AFTER it count towards the same guard again. The command never creates
a table (if `strategy_pauses` does not exist it says so and does nothing), never places or cancels an order, and prints no secret.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from .config import load_settings
from .models import StrategyPause
from .strategy_guards import is_active
from .telemetry import STRATEGY_ID


def _session(url: str):
    engine = create_engine(url, connect_args={"timeout": 8.0} if url.startswith("sqlite") else {})
    if not inspect(engine).has_table("strategy_pauses"):
        return engine, None
    return engine, Session(engine)


def _line(p: StrategyPause, now: datetime) -> str:
    ends = "until released" if p.resume_at is None else f"until {p.resume_at.isoformat(timespec='minutes')}"
    state = "ACTIVE" if is_active(p, now) else ("released" if p.released_at else "ended")
    return (f"{state:8s} account {p.account_id[:8]}...  {p.strategy_id}  {'paper' if p.is_paper else 'live '}  {p.code}  "
            f"since {p.paused_at.isoformat(timespec='minutes')}  {ends}  [{p.detail or '-'}]")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="strategy_pause", description="Show or release strategy pauses.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    rel = sub.add_parser("release")
    rel.add_argument("--account", required=True, help="account id, or its first 8 or more characters")
    rel.add_argument("--strategy", default=STRATEGY_ID)
    rel.add_argument("--mode", choices=("paper", "live", "both"), default="both")
    rel.add_argument("--note", required=True)
    rel.add_argument("--confirm", required=True, help="type RELEASE")
    args = ap.parse_args(argv)
    settings = load_settings()
    engine, session = _session(settings.database_url)
    if session is None:
        print("There is no strategy_pauses table yet: nothing is paused and nothing was changed.")
        return 0
    now = datetime.now(timezone.utc)
    with session:
        if args.cmd == "status":
            rows = list(session.scalars(select(StrategyPause).order_by(StrategyPause.paused_at.desc()).limit(50)))
            active = [r for r in rows if is_active(r, now)]
            print(f"{len(active)} active pause(s) (latest 50 rows shown):")
            for r in rows:
                print("  " + _line(r, now))
            return 0
        if args.confirm != "RELEASE":
            print("Refused: type --confirm RELEASE to release a pause.", file=sys.stderr)
            return 2
        if len(args.account) < 8:
            print("Refused: give at least the first 8 characters of the account id.", file=sys.stderr)
            return 2
        matches = {r.account_id for r in session.scalars(select(StrategyPause).where(StrategyPause.account_id.like(args.account + "%")))}
        if len(matches) != 1:
            print(f"Refused: {len(matches)} accounts match that id; nothing was changed.", file=sys.stderr)
            return 2
        account_id = matches.pop()
        n = 0
        for p in session.scalars(select(StrategyPause).where(StrategyPause.account_id == account_id, StrategyPause.strategy_id == args.strategy,
                                                             StrategyPause.released_at.is_(None))):
            if args.mode != "both" and p.is_paper != (args.mode == "paper"):
                continue
            p.released_at = now
            p.detail = ((p.detail or "") + f" | released: {args.note}")[:200]
            n += 1
        session.commit()
        print(f"Released {n} pause(s) for account {account_id[:8]}... ({args.strategy}, {args.mode}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
