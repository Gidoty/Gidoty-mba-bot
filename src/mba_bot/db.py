"""Engine/session setup plus the handful of queries the scheduler needs.

No migrations tool wired in yet (see README) - `init_db` just calls
`create_all`, which is fine for SQLite/early Postgres use but should be
replaced with Alembic before the schema needs to change under live data.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, select
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
    return list(session.scalars(select(TradingAccount).where(TradingAccount.is_active.is_(True))))


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
