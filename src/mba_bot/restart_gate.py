"""Read-only restart gate: exit code 0 only if NO live trade is open, so a deploy or restart script can refuse to restart otherwise.

    python -m mba_bot.restart_gate

It reads the database and writes nothing, places no order and calls no exchange. It prints counts only (no account id, no pair, no
customer data). With more than one live trade per account possible in the database (multi_trade.py), the check counts every
non-terminal LIVE trade, never "accounts with a trade".

Exit codes: 0 no live trade open, 3 at least one live trade is open (do NOT restart), 2 the database could not be read (treat as do NOT
restart). The owner's own deploy script decides what to do with the code; this file changes no service.
"""
from __future__ import annotations

import sys
from collections import Counter

from .db import list_live_unsettled_trades, session_scope

EXIT_CLEAR, EXIT_UNREADABLE, EXIT_LIVE_TRADES_OPEN = 0, 2, 3


def live_trade_counts() -> Counter:
    with session_scope() as session:
        return Counter(t.status for t in list_live_unsettled_trades(session))


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
