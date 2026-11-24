"""The customer's own trade statement as a CSV download (docs/safety/statements_and_closure.md). Needs a sign-in, shows only the signed-in customer's own trades, and writes nothing."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from ... import reporting, statement
from ...db import list_trades_for_account
from ...models import Customer
from ..deps import get_current_customer, get_db

router = APIRouter(prefix="/api/statements", tags=["statements"])


def _day(raw: str | None, name: str) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.combine(date.fromisoformat(raw), datetime.min.time())
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{name} must be a date like 2026-01-31") from None


@router.get("/trades.csv")
def trades_csv(mode: str | None = Query(default=None), start: str | None = Query(default=None, alias="from"), end: str | None = Query(default=None, alias="to"),
               customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)) -> Response:
    if mode is not None and mode not in (reporting.MODE_LIVE, reporting.MODE_DEMO, reporting.MODE_PAPER):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "mode must be live, demo or paper")
    account = customer.account
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no trading account yet")
    d0, d1 = _day(start, "from"), _day(end, "to")
    if d0 and d1 and d1 < d0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "to is before from")
    text, truncated = statement.build_csv(list_trades_for_account(db, account.id), mode=mode, start=d0, end=d1)
    headers = {"Content-Disposition": f'attachment; filename="trades-{re.sub(r"[^A-Za-z0-9-]", "", account.bot_code or "") or "account"}-{datetime.now(timezone.utc):%Y%m%d}.csv"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if truncated:
        headers["X-Statement-Truncated"] = "1"
    return Response(content=text, media_type="text/csv; charset=utf-8", headers=headers)
