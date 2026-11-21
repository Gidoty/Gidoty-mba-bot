"""Customer data requests: ask for a copy of your data (export) or for it to be deleted (deletion).

The smallest safe version: an authenticated request is RECORDED for the operator (table data_requests). Nothing is exported or deleted by this route, and nothing is deleted automatically anywhere in the
service. The operator reads open requests with `python -m mba_bot.data_requests list` and prepares an export with `python -m mba_bot.data_requests export`. The route needs a login but NOT the current
Terms acceptance: a customer who has not accepted a new version can still ask for their data.

Records that are kept even after an approved deletion, and why, are listed in RETAINED_RECORDS; the Privacy Policy text repeats them and a test checks the two agree."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...legal import OPERATOR_EMAIL
from ...models import Customer, DataRequest
from ..deps import get_current_customer, get_db

router = APIRouter(prefix="/api/privacy", tags=["privacy"])

KINDS = ("export", "deletion")

# (record, why it is kept). The Privacy Policy lists the same records.
RETAINED_RECORDS = (
    ("payment and subscription records", "to reconcile payments, handle refunds and disputes, and meet accounting duties"),
    ("your acceptances of the Terms, Privacy Policy and Risk Disclosure, with version and time", "as proof of what you agreed to and when"),
    ("the record of your request and its outcome", "as proof that it was made and handled"),
)


class DataRequestIn(BaseModel):
    kind: str


class DataRequestOut(BaseModel):
    id: str
    kind: str
    status: str
    requested_at: datetime
    message: str


def _message(kind: str) -> str:
    if kind == "export":
        return f"We recorded your request for a copy of your data. The operator will prepare it and reply to the e-mail on your account. Questions: {OPERATOR_EMAIL}"
    kept = "; ".join(f"{what} ({why})" for what, why in RETAINED_RECORDS)
    return (
        "We recorded your request to delete your data. Nothing is deleted automatically: the operator reviews the request, deletes your account data by hand and replies to the e-mail on your account. "
        f"Records we must keep even then: {kept}. Questions: {OPERATOR_EMAIL}"
    )


def _out(row: DataRequest) -> DataRequestOut:
    return DataRequestOut(id=row.id, kind=row.kind, status=row.status, requested_at=row.requested_at, message=_message(row.kind))


@router.post("/requests", response_model=DataRequestOut)
def create_request(body: DataRequestIn, customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)) -> DataRequestOut:
    """One open request per customer and kind: asking again returns the open one, so a double click or a retry writes nothing new."""
    if body.kind not in KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "kind must be export or deletion")
    existing = db.scalar(select(DataRequest).where(DataRequest.customer_id == customer.id, DataRequest.kind == body.kind, DataRequest.status == "open"))
    if existing is not None:
        return _out(existing)
    row = DataRequest(customer_id=customer.id, kind=body.kind, status="open")
    db.add(row)
    db.flush()
    return _out(row)


@router.get("/requests", response_model=list[DataRequestOut])
def my_requests(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)) -> list[DataRequestOut]:
    rows = db.scalars(select(DataRequest).where(DataRequest.customer_id == customer.id).order_by(DataRequest.requested_at.desc()).limit(20)).all()
    return [_out(r) for r in rows]
