"""Operator tool for customer data requests (docs/commercial_readiness.md). Read-only: it never deletes or changes anything.

    python -m mba_bot.data_requests list                       open requests: id, kind, customer short id, date (no e-mail)
    python -m mba_bot.data_requests export --customer <id>     one customer's data as JSON, to --out FILE (never overwrites) or the screen

The export holds what the service stores about the customer: e-mail, sign-up date, account settings, trade records, acceptance records, subscription and payment summaries. It never holds the password hash,
the encrypted exchange keys, a Telegram chat id or a provider reference or token. The database is opened READ-ONLY (`PRAGMA query_only`); a missing file is an error and nothing is created.
Deleting data is NOT done here: it is a manual decision by the operator (see the Privacy Policy for the records that are kept). Exit codes: 0 done, 2 could not read or no such customer."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BillingPayment, Customer, DataRequest, LegalAcceptance, Subscription, Trade, TradingAccount
from .restart_gate import DEFAULT_URL, _engine

ACCOUNT_FIELDS = ("bot_code", "pair", "market_type", "pair_selection_mode", "max_leverage", "risk_pct", "allocated_capital", "daily_loss_limit_usd", "notification_channel", "created_at", "trial_ends_at",
                  "live_trading_enabled_at", "bot_started_at")
TRADE_FIELDS = ("id", "pair", "side", "strategy_id", "status", "is_paper", "opened_at", "closed_at", "entry_price", "stop_loss_price", "exit_price", "quantity", "pnl", "close_reason")
PAYMENT_FIELDS = ("provider", "amount_minor", "currency", "usd_value", "status", "issued_at", "paid_at", "period_start", "period_end")  # no reference, no provider payment id
SUBSCRIPTION_FIELDS = ("status", "provider", "current_period_start", "current_period_end", "grace_until", "created_at")


def _plain(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _pick(row, fields) -> dict:
    return {f: _plain(getattr(row, f, None)) for f in fields}


def _open_session(url: str | None):
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url.startswith("sqlite") and ":memory:" not in url:
        path = url.split("///", 1)[-1].split("?", 1)[0]
        if path and not os.path.exists(path):
            raise FileNotFoundError("database file not found")  # opening a missing file would create it
    return _engine(url)


def open_requests(session: Session) -> list[str]:
    rows = session.scalars(select(DataRequest).where(DataRequest.status == "open").order_by(DataRequest.requested_at)).all()
    return [f"{r.id[:8]}  {r.kind:<8} customer {r.customer_id[:8]}  requested {_plain(r.requested_at)}" for r in rows] or ["no open requests"]


def export_customer(session: Session, customer_id: str) -> dict | None:
    matches = session.scalars(select(Customer).where(Customer.id.like(customer_id + "%"))).all()
    if len(matches) != 1:
        return None
    c = matches[0]
    out: dict = {"customer": {"email": c.email, "created_at": _plain(c.created_at), "is_active": c.is_active, "activated": c.has_paid}}
    account = session.scalars(select(TradingAccount).where(TradingAccount.customer_id == c.id)).first()
    out["account"] = _pick(account, ACCOUNT_FIELDS) if account else None
    out["telegram_linked"] = bool(account and account.notification_target)
    out["trades"] = [_pick(t, TRADE_FIELDS) for t in session.scalars(select(Trade).where(Trade.account_id == account.id).order_by(Trade.opened_at)).all()] if account else []
    out["acceptances"] = [{"document": a.document_type, "version": a.version, "accepted_at": _plain(a.accepted_at)} for a in session.scalars(select(LegalAcceptance).where(LegalAcceptance.customer_id == c.id).order_by(LegalAcceptance.accepted_at)).all()]
    sub = session.scalars(select(Subscription).where(Subscription.customer_id == c.id)).first()
    out["subscription"] = _pick(sub, SUBSCRIPTION_FIELDS) if sub else None
    out["payments"] = [_pick(p, PAYMENT_FIELDS) for p in session.scalars(select(BillingPayment).where(BillingPayment.customer_id == c.id).order_by(BillingPayment.issued_at)).all()]
    out["data_requests"] = [{"kind": r.kind, "status": r.status, "requested_at": _plain(r.requested_at)} for r in session.scalars(select(DataRequest).where(DataRequest.customer_id == c.id)).all()]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.data_requests")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    ex = sub.add_parser("export")
    ex.add_argument("--customer", required=True, help="the customer's id, or its first 8 or more characters")
    ex.add_argument("--out", help="write the JSON to this file (never overwrites) instead of the screen")
    args = ap.parse_args(argv)
    try:
        engine = _open_session(None)
        try:
            with Session(engine) as session:
                if args.cmd == "list":
                    print("\n".join(open_requests(session)))
                    return 0
                if len(args.customer) < 8:
                    print("data requests: give at least 8 characters of the customer id", file=sys.stderr)
                    return 2
                data = export_customer(session, args.customer)
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - fixed class name only, never the message
        print(f"data requests: cannot read the database ({type(exc).__name__})", file=sys.stderr)
        return 2
    if data is None:
        print("data requests: no single customer matches that id", file=sys.stderr)
        return 2
    text = json.dumps(data, indent=2, sort_keys=True, default=str)
    if args.out:
        try:
            with open(args.out, "x") as fh:  # "x": never overwrites an existing file
                fh.write(text + "\n")
        except OSError as exc:
            print(f"data requests: cannot write {args.out} ({type(exc).__name__})", file=sys.stderr)
            return 2
        print(f"data requests: wrote the export to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
