"""Read-only restart gate: exit code 0 only if NO live trade is open, so a deploy or restart script can refuse to restart otherwise.

    python -m mba_bot.restart_gate        (from the app directory, with DATABASE_URL as the service has it)

It opens its OWN connection from DATABASE_URL (default sqlite:///./mba_bot.db), the way `strategy_pause` does: no init_db, no create_all, no table
is ever created, and on SQLite the connection is `PRAGMA query_only` so it cannot write. It places no order and calls no exchange. It prints counts only
(no account id, no pair, no customer data). It counts every non-terminal LIVE trade, never "accounts with a trade" (a paper account may hold several
paper trades; they never block a restart).

Exit codes: 0 no live trade open, 3 at least one live trade is open (do NOT restart), 2 the database could not be read (a missing file, a missing
table, a locked database: treat it as do NOT restart). The owner's own deploy script decides what to do with the code; this file changes no service.
"""
from __future__ import annotations

import os
import sys
from collections import Counter

from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.orm import Session

from .models import NON_TERMINAL_TRADE_STATUSES, Trade

EXIT_CLEAR, EXIT_UNREADABLE, EXIT_LIVE_TRADES_OPEN = 0, 2, 3
DEFAULT_URL = "sqlite:///./mba_bot.db"


def _engine(url: str):
    if not url.startswith("sqlite"):
        return create_engine(url)
    engine = create_engine(url, connect_args={"timeout": 8.0})

    @event.listens_for(engine, "connect")
    def _query_only(dbapi_connection, _record):  # noqa: ANN001
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA query_only = ON")
        cur.close()

    return engine


def live_trade_counts(url: str | None = None) -> Counter:
    """Non-terminal LIVE trades by status. Raises if the database cannot be read (the caller treats that as 'do not restart')."""
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url.startswith("sqlite") and ":memory:" not in url:
        path = url.split("///", 1)[-1].split("?", 1)[0]
        if path and not os.path.exists(path):
            raise FileNotFoundError("database file not found")  # a missing file must not be created by opening it
    engine = _engine(url)
    try:
        if not inspect(engine).has_table(Trade.__tablename__):
            raise LookupError("trades table not found")
        with Session(engine) as session:
            rows = session.execute(
                select(Trade.status, func.count()).where(Trade.is_paper.is_(False), Trade.status.in_(NON_TERMINAL_TRADE_STATUSES)).group_by(Trade.status)
            ).all()
        return Counter({status: n for status, n in rows})
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        counts = live_trade_counts()
    except Exception as exc:  # noqa: BLE001 - fail closed: an unreadable database is not "clear"
        print(f"restart gate: cannot read the database ({type(exc).__name__}); do NOT restart", file=sys.stderr)
        return EXIT_UNREADABLE
    total = sum(counts.values())
    if total == 0:
        print("restart gate: no live trade open")
        return EXIT_CLEAR
    print(f"restart gate: {total} live trade(s) open ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}); do NOT restart")
    return EXIT_LIVE_TRADES_OPEN


if __name__ == "__main__":
    raise SystemExit(main())
