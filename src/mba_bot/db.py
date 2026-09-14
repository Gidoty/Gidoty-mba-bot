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
    return [t for t in trades if t.closed_at and t.closed_at.astimezone(timezone.utc).strftime("%Y-%m-%d") == date_str]
