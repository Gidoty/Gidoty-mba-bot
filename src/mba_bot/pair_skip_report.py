"""Read-only report of the pairs the bot skipped for being too small for an account's capital, last 7 days (docs/safety/small_capital_pairs.md).

    python -m mba_bot.pair_skip_report [--days N]

It opens the database READ-ONLY (`PRAGMA query_only`, like `restart_gate`), makes no exchange call, writes nothing and prints no key, secret, e-mail or full account id (the first 8 characters only).
The table is filled only while SMALL_CAPITAL_PAIRS is on, so an empty report with the switch off is expected. Exit codes: 0 printed, 2 unreadable."""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import PairSkip
from .restart_gate import DEFAULT_URL, _engine


def render(url: str | None = None, *, days: int = 7, now: datetime | None = None) -> list[str]:
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
    finally:
        engine.dispose()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pair_skip_report")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    try:
        lines = render(days=max(1, min(90, args.days)))
    except Exception as exc:  # noqa: BLE001 - fixed class name only, never the message
        print(f"pair skip report: cannot start ({type(exc).__name__})", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
