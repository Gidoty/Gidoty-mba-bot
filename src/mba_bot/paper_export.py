"""Read-only export of PAPER trades to CSV, for the paper-versus-mainnet comparison (docs/ops/paper_source_and_replay.md).

    python -m mba_bot.paper_export [--out paper_trades.csv]        (from the app directory, with DATABASE_URL as the service has it)

Opens the database READ-ONLY (`PRAGMA query_only`, like restart_gate): no init_db, nothing created or written; a missing file is an error. It selects the same columns as docs/ops/paper_trades_export.sql:
a short trade reference, a 4-character account prefix (not the account id), market, pair, side, strategy, times, prices, quantity, pnl, close reason and the risk amount. No e-mail, no account id, no key.
Exit codes: 0 written, 2 the database could not be read."""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Trade, TradingAccount
from .restart_gate import DEFAULT_URL, _engine

COLUMNS = ("trade_ref", "account_ref", "market_type", "pair", "side", "strategy_id", "status", "opened_at", "closed_at", "entry_price", "stop_loss_price", "exit_price", "quantity", "pnl", "close_reason",
           "risk_amount")


def _iso(value) -> str:
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


def export_rows(url: str | None = None) -> list[dict]:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url.startswith("sqlite") and ":memory:" not in url:
        path = url.split("///", 1)[-1].split("?", 1)[0]
        if path and not os.path.exists(path):
            raise FileNotFoundError("database file not found")
    engine = _engine(url)
    try:
        with Session(engine) as session:
            rows = session.execute(select(Trade, TradingAccount).join(TradingAccount, TradingAccount.id == Trade.account_id).where(Trade.is_paper.is_(True)).order_by(Trade.opened_at)).all()
            out = []
            for t, a in rows:
                out.append({
                    "trade_ref": t.id[:8], "account_ref": a.id[:4], "market_type": a.market_type, "pair": t.pair or a.pair, "side": t.side, "strategy_id": t.strategy_id or "ema_cross_atr_v1",
                    "status": t.status, "opened_at": _iso(t.opened_at), "closed_at": _iso(t.closed_at), "entry_price": t.entry_price, "stop_loss_price": t.stop_loss_price, "exit_price": "" if t.exit_price is None else t.exit_price,
                    "quantity": t.quantity, "pnl": "" if t.pnl is None else t.pnl, "close_reason": t.close_reason or "", "risk_amount": abs(t.entry_price - t.stop_loss_price) * t.quantity,
                })
            return out
    finally:
        engine.dispose()


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.paper_export")
    ap.add_argument("--out", help="write the CSV to this file instead of the screen")
    args = ap.parse_args(argv)
    try:
        text = to_csv(export_rows())
    except Exception as exc:  # noqa: BLE001 - fixed class name only
        print(f"paper export: cannot read the database ({type(exc).__name__})", file=sys.stderr)
        return 2
    if args.out:
        with open(args.out, "x", newline="") as fh:  # never overwrites an existing file
            fh.write(text)
        print(f"paper export: wrote {text.count(chr(10)) - 1} paper trade(s) to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
