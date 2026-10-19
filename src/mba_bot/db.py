"""Engine/session setup plus the handful of queries the scheduler needs.

Schema changes on a deployed database go through Alembic
(`alembic upgrade head` - see `alembic/` and the README), not through
this module. `init_db`'s own `create_all` only ever creates missing
*tables*; it never adds a column to one that already exists, which is
exactly what broke in production once (see git history) when a column
was added to `Customer` after the live database already had that table.
It stays here purely as dev/test convenience - every test builds a
throwaway SQLite file from scratch, where `create_all` and
`alembic upgrade head` produce an identical result, so paying Alembic's
overhead on every test run buys nothing.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Trade, TradingAccount

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def init_db(database_url: str) -> None:
    global _engine, _SessionLocal
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_engine(database_url, connect_args=connect_args)
    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        raise RuntimeError("init_db() must be called before using the database")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def list_active_accounts(session: Session) -> list[TradingAccount]:
    """Accounts the scheduler should actually process this tick: anything
    not switched off internally (`is_active` - there is no customer-facing
    Pause any more, this is an admin/internal kill switch only, see
    api/routers/account.py), OR, regardless of that flag, any account with
    a currently open trade. That second half is load-bearing, not
    defensive: `is_active=False` stops `_run_one` from calling
    `process_account` at all, and an open position with nobody calling
    `process_account` gets zero stop-loss/crossover-exit monitoring and
    zero chance to close - exactly the "orphaned real position" failure
    mode this project treats as unacceptable. So a position being open
    always wins over the kill switch; the switch still fully prevents
    *new* entries on an inactive account, since `_maybe_enter` is only
    ever reached when there is no open trade.

    Payment is deliberately NOT a filter here either - an unpaid account
    still runs, it just never gets past PAPER, because `resolve_trading_mode`
    (onboarding.py) is where `Customer.has_paid` (and, since the Start Bot
    gate, `bot_started_at`) is actually enforced, keyed off this same
    account's `customer` relationship. Keeping that check in exactly one
    place means a free, unlimited paper trial for anyone and a hard
    payment/start wall in front of LIVE can't drift out of sync with each
    other. See ARCHITECTURE.md."""
    open_trade_account_ids = select(Trade.account_id).where(Trade.status == "open")
    return list(
        session.scalars(
            select(TradingAccount).where(
                or_(TradingAccount.is_active.is_(True), TradingAccount.id.in_(open_trade_account_ids))
            )
        )
    )


def get_open_trade(session: Session, account_id: str) -> Trade | None:
    return session.scalars(
        select(Trade).where(Trade.account_id == account_id, Trade.status == "open")
    ).first()


def get_trades_closed_on(session: Session, account_id: str, date_str: str) -> list[Trade]:
    """`date_str` is a UTC calendar date, e.g. '2026-09-14'."""
    trades = session.scalars(
        select(Trade).where(Trade.account_id == account_id, Trade.status == "closed")
    ).all()
    matches = []
    for t in trades:
        if not t.closed_at:
            continue
        # SQLite drops tzinfo on round-trip - see onboarding.py for the
        # same issue. A naive value here is UTC by convention, so
        # `.astimezone(timezone.utc)` on it would otherwise misinterpret
        # it as local time instead of being a no-op.
        closed_at = t.closed_at if t.closed_at.tzinfo else t.closed_at.replace(tzinfo=timezone.utc)
        if closed_at.astimezone(timezone.utc).strftime("%Y-%m-%d") == date_str:
            matches.append(t)
    return matches


def list_trades_for_account(session: Session, account_id: str) -> list[Trade]:
    return list(
        session.scalars(select(Trade).where(Trade.account_id == account_id).order_by(Trade.opened_at.desc()))
    )


def get_account_by_telegram_link_token(session: Session, token: str) -> TradingAccount | None:
    return session.scalars(select(TradingAccount).where(TradingAccount.telegram_link_token == token)).first()
