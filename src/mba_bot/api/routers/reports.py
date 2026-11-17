"""GET /api/reports/summary: the logged-in customer's own results by mode, strategy and pair (docs/safety/reporting.md). Read-only. Behind REPORTING_V2 (default off: 409)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ... import reporting
from ...config import Settings
from ...db import list_trades_for_account
from ...models import Customer
from ..deps import get_current_customer, get_db, get_settings

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/summary")
def summary(
    window_days: int | None = Query(default=None, ge=1, le=3650),
    customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if not settings.reporting_v2:
        raise HTTPException(status.HTTP_409_CONFLICT, "reports are not available on this service yet")
    account = customer.account
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no trading account yet")
    trades = list_trades_for_account(db, account.id)  # this customer's account only: there is no account parameter
    if window_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

        def closed_after(t) -> bool:
            if t.closed_at is None:
                return False
            c = t.closed_at if t.closed_at.tzinfo else t.closed_at.replace(tzinfo=timezone.utc)
            return c >= cutoff

        trades = [t for t in trades if closed_after(t)]
    out = reporting.build(trades)
    out["window_days"] = window_days
    out["quote_currency"] = (account.pair.split("/")[-1] if "/" in account.pair else None)
    return out
