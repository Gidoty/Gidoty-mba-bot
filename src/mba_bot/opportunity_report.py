"""Read-only opportunity report: what each strategy saw and why entries were or were not taken (docs/safety/opportunity_ledger.md).

    python -m mba_bot.opportunity_report [--hours 24] [--account 27624db9] [--candles]

Per account (first 8 characters of the id) and strategy: candles evaluated, signals found, entries opened, and every non-entry with its reason, grouped. The evaluation counts come from the opportunity ledger
(OPPORTUNITY_LEDGER); entries opened and refusals also come from the existing entry-stage decision rows (TRADE_TELEMETRY). It opens the database READ-ONLY, writes nothing and prints no key, secret, e-mail or
full account id. A 'ties' section (opportunity_ties.py) counts rounds where two or more pairs signalled the same strategy and shows each pair's signal strength; ONLY with --candles it also reads PUBLIC Binance
candles (no key, one read per pair) for what the price did over the next 24 candles in the signal's direction. Without --candles no exchange call is made. Exit codes: 0 printed, 2 unreadable."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import opportunity, opportunity_ties
from .models import DecisionLog
from .restart_gate import DEFAULT_URL, _engine


def render(url: str | None = None, *, hours: int = 24, account: str | None = None, now: datetime | None = None, fetch=None) -> list[str]:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url.startswith("sqlite") and ":memory:" not in url:
        path = url.split("///", 1)[-1].split("?", 1)[0]
        if path and not os.path.exists(path):
            raise FileNotFoundError("database file not found")  # opening a missing file would create it
    now = now or datetime.now(timezone.utc)
    engine = _engine(url)
    out = [f"opportunity report, last {hours} hours (read-only; evaluation counts need OPPORTUNITY_LEDGER, entries and refusals need TRADE_TELEMETRY)"]
    try:
        with Session(engine) as session:
            rows = opportunity.collect(session, since=now - timedelta(hours=hours))
            if account:
                rows = [r for r in rows if r["account_id"].startswith(account)]
            ledger_rows = sum(1 for r in rows if r["stage"] == opportunity.STAGE)
            out.append(f"ledger rows in the window: {ledger_rows}" + ("" if ledger_rows else " (OPPORTUNITY_LEDGER is off, or nothing was evaluated yet)"))
            tie_rows = opportunity_ties.collect(session, since=now - timedelta(hours=hours), account_prefix=account)
            tie_lines = opportunity_ties.render(opportunity_ties.analyse(tie_rows), fetch=fetch, now=now, hours=hours)
            out.insert(1, tie_lines[0])  # the count goes at the top
            data = opportunity.aggregate(rows)
            if not data:
                out.append("nothing recorded")
                out.extend(tie_lines[1:])
                return out
            for account_id in sorted(data):
                out.append("")
                out.append(f"account {account_id[:8]}")
                for sid in sorted(data[account_id], key=lambda k: (k not in opportunity.LABEL, list(opportunity.LABEL).index(k) if k in opportunity.LABEL else 99, k)):
                    s = data[account_id][sid]
                    out.append(f"  {opportunity.LABEL.get(sid, sid)} ({sid}): candles evaluated {s['evaluated']}, signals {s['signals']}, entries opened {s['opened']}")
                    if s["not_taken"]:
                        out.append("    not taken: " + "; ".join(f"{c} {n} ({opportunity.plain(c)})" for c, n in sorted(s["not_taken"].items(), key=lambda kv: (-kv[1], kv[0]))))
                    if s["no_signal"]:
                        out.append("    no signal: " + "; ".join(f"{c} {n} ({opportunity.plain(c)})" for c, n in sorted(s["no_signal"].items(), key=lambda kv: (-kv[1], kv[0]))))
            total = session.execute(select(func.count()).select_from(DecisionLog).where(DecisionLog.stage == opportunity.STAGE)).scalar_one()
            out.append("")
            out.extend(tie_lines[1:])
            out.append("")
            out.append(f"ledger rows in the table in total: {total}")
    finally:
        engine.dispose()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="opportunity_report")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--account", help="only this account (first characters of its id)")
    ap.add_argument("--candles", action="store_true", help="read PUBLIC Binance candles (no key) for the price moves after each tie; without it no exchange call is made")
    args = ap.parse_args(argv)
    try:
        lines = render(hours=max(1, min(720, args.hours)), account=args.account, fetch=opportunity_ties.public_candles if args.candles else None)
    except Exception as exc:  # noqa: BLE001 - fixed class name only, never the message
        print(f"opportunity report: cannot start ({type(exc).__name__})", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
