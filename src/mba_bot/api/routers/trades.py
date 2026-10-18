from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...db import list_trades_for_account
from ...models import Customer, Trade
from ..deps import get_current_customer, get_db
from ..schemas import TradeOut, TradesSummaryOut

router = APIRouter(prefix="/api/trades", tags=["trades"])


def _account_or_404(customer: Customer):
    if customer.account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no trading account yet")
    return customer.account


def _filter_is_paper(trades: list[Trade], is_paper: bool | None) -> list[Trade]:
    if is_paper is None:
        return trades
    return [t for t in trades if t.is_paper == is_paper]


@router.get("", response_model=list[TradeOut])
def list_trades(
    is_paper: bool | None = None,
    customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> list[TradeOut]:
    """`is_paper` is optional so any existing caller that doesn't pass it
    keeps getting every trade, same as before this filter existed - the
    Testnet and Live pages are the only callers that need to pass it."""
    account = _account_or_404(customer)
    return _filter_is_paper(list_trades_for_account(db, account.id), is_paper)


def _pnl_since(closed_trades: list[Trade], *, cutoff: datetime | None) -> float:
    total = 0.0
    for t in closed_trades:
        if t.pnl is None or t.closed_at is None:
            continue
        # SQLite drops tzinfo on round-trip (see onboarding.py/db.py for
        # the same issue elsewhere) - a naive value here is UTC by
        # convention.
        closed_at = t.closed_at if t.closed_at.tzinfo else t.closed_at.replace(tzinfo=timezone.utc)
        if cutoff is None or closed_at >= cutoff:
            total += t.pnl
    return total


@router.get("/summary", response_model=TradesSummaryOut)
def trades_summary(
    is_paper: bool | None = None,
    customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> TradesSummaryOut:
    """Scoped by `is_paper` when passed, so a customer's Testnet
    statistics and Live statistics are never mixed into the same number
    - the Testnet Dashboard always passes `is_paper=true`, the Live
    Dashboard always passes `is_paper=false`. Omitting it (no existing
    caller does) falls back to the old unscoped behaviour."""
    account = _account_or_404(customer)
    trades = _filter_is_paper(list_trades_for_account(db, account.id), is_paper)
    closed = [t for t in trades if t.status == "closed"]
    open_ = [t for t in trades if t.status == "open"]
    net_pnl = sum(t.pnl or 0.0 for t in closed)
    wins = sum(1 for t in closed if (t.pnl or 0.0) > 0)
    win_rate = (wins / len(closed)) if closed else None

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return TradesSummaryOut(
        total_trades=len(trades),
        open_trades=len(open_),
        closed_trades=len(closed),
        net_pnl=net_pnl,
        win_rate=win_rate,
        pnl_today=_pnl_since(closed, cutoff=today_start),
        pnl_7d=_pnl_since(closed, cutoff=now - timedelta(days=7)),
        pnl_30d=_pnl_since(closed, cutoff=now - timedelta(days=30)),
        pnl_90d=_pnl_since(closed, cutoff=now - timedelta(days=90)),
    )
