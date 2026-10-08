from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...db import list_trades_for_account
from ...models import Customer
from ..deps import get_current_customer, get_db
from ..schemas import TradeOut, TradesSummaryOut

router = APIRouter(prefix="/api/trades", tags=["trades"])


def _account_or_404(customer: Customer):
    if customer.account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no trading account yet")
    return customer.account


@router.get("", response_model=list[TradeOut])
def list_trades(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)) -> list[TradeOut]:
    account = _account_or_404(customer)
    return list_trades_for_account(db, account.id)


@router.get("/summary", response_model=TradesSummaryOut)
def trades_summary(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)) -> TradesSummaryOut:
    account = _account_or_404(customer)
    trades = list_trades_for_account(db, account.id)
    closed = [t for t in trades if t.status == "closed"]
    open_ = [t for t in trades if t.status == "open"]
    net_pnl = sum(t.pnl or 0.0 for t in closed)
    wins = sum(1 for t in closed if (t.pnl or 0.0) > 0)
    win_rate = (wins / len(closed)) if closed else None
    return TradesSummaryOut(
        total_trades=len(trades), open_trades=len(open_), closed_trades=len(closed), net_pnl=net_pnl, win_rate=win_rate
    )
