"""Read-only report of the pairs the bot skipped for being too small for an account's capital, last 7 days (docs/safety/small_capital_pairs.md), and of the REFUSED ENTRIES of the last 24 hours per pair and
reason (so a missed entry can be seen without reading raw logs; docs/safety/pre_entry_protection_check.md).

    python -m mba_bot.pair_skip_report [--days N] [--hours N]

It opens the database READ-ONLY (`PRAGMA query_only`, like `restart_gate`), makes no exchange call, writes nothing and prints no key, secret, e-mail or full account id (the first 8 characters only).
The skip table is filled only while SMALL_CAPITAL_PAIRS is on, so an empty report with the switch off is expected. The refusal counts come from the decision log (rows with outcome `rejected`, one per refused
candle), which is written only while TRADE_TELEMETRY is on; the report says so when there are none. Exit codes: 0 printed, 2 unreadable."""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import DecisionLog, PairSkip
from .restart_gate import DEFAULT_URL, _engine


def refusal_lines(session: Session, *, hours: int, now: datetime) -> list[str]:
    """Refused entries (decision log, outcome `rejected`) in the last `hours` hours: per pair and reason, the number of refused candles and the accounts (first 8 characters). Read-only."""
    since = now - timedelta(hours=hours)
    rows = session.execute(
        select(DecisionLog.pair, DecisionLog.reason_code, func.count(), func.count(func.distinct(DecisionLog.account_id)), func.max(DecisionLog.ts))
        .where(DecisionLog.outcome == "rejected", DecisionLog.stage == "entry", DecisionLog.ts >= since)
        .group_by(DecisionLog.pair, DecisionLog.reason_code).order_by(func.count().desc(), DecisionLog.pair, DecisionLog.reason_code)
    ).all()
    out = [f"refused entries, last {hours} hours (decision log: needs TRADE_TELEMETRY; one row per refused candle)"]
    if not rows:
        out.append("no refused entries recorded")
        return out
    totals: dict[str, int] = {}
    for pair, reason, n, accounts, last in rows:
        totals[reason] = totals.get(reason, 0) + n
    out.append("by reason: " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    for pair, reason, n, accounts, last in rows:
        stamp = last.strftime("%Y-%m-%d %H:%M UTC") if hasattr(last, "strftime") else "?"
        out.append(f"  {(pair or '-'):<10} {reason:<42} refused {n:>4}  accounts {accounts}  last {stamp}")
    return out


def render(url: str | None = None, *, days: int = 7, hours: int = 24, now: datetime | None = None) -> list[str]:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url.startswith("sqlite") and ":memory:" not in url:
        path = url.split("///", 1)[-1].split("?", 1)[0]
        if path and not os.path.exists(path):
            raise FileNotFoundError("database file not found")  # opening a missing file would create it
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    engine = _engine(url)
    out = [f"pair skip report, last {days} days (read-only; filled only while SMALL_CAPITAL_PAIRS is on)"]
    try:
        with Session(engine) as session:
            rows = session.execute(select(PairSkip).where(PairSkip.last_seen >= since).order_by(PairSkip.reason, PairSkip.pair, PairSkip.account_id)).scalars().all()
            if not rows:
                out.append("no skipped pairs recorded")
                out += refusal_lines(session, hours=hours, now=now)
                return out
            by_reason: dict[str, int] = {}
            for r in rows:
                by_reason[r.reason] = by_reason.get(r.reason, 0) + (r.count or 0)
            out.append("by reason (skips): " + ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))
            for reason in sorted(by_reason):
                out.append(f"{reason}:")
                grouped: dict[str, list[PairSkip]] = {}
                for r in rows:
                    if r.reason == reason:
                        grouped.setdefault(r.pair, []).append(r)
                for pair, items in sorted(grouped.items()):
                    needs = [r.min_capital for r in items if r.min_capital is not None]
                    accounts = sorted({r.account_id[:8] for r in items})
                    out.append(f"  {pair:<10} skips {sum(r.count or 0 for r in items):>4}  accounts {', '.join(accounts)}  "
                               f"capital needed {('up to %.2f USDT' % (math.ceil(max(needs) * 100) / 100)) if needs else 'unknown'}")
            out += refusal_lines(session, hours=hours, now=now)
    finally:
        engine.dispose()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pair_skip_report")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args(argv)
    try:
        lines = render(days=max(1, min(90, args.days)), hours=max(1, min(720, args.hours)))
    except Exception as exc:  # noqa: BLE001 - fixed class name only, never the message
        print(f"pair skip report: cannot start ({type(exc).__name__})", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
