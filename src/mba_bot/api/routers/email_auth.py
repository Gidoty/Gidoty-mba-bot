"""Email verification and password reset routes (docs/safety/email_and_recovery.md). Always mounted; with EMAIL_TRANSPORT off (the default) they say plainly that email is not set up and change nothing.

No user enumeration: the reset request answers the same words for an address with an account and one without, and does the same work before answering (the mail is sent after the response). Per-address and
per-customer limits are silent (the same answer); the per-address-of-the-caller limit answers 429 for everyone alike."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import email_flows as F
from ...config import Settings
from ...db import session_scope
from ...mailer import build_mailer, configured
from ...models import Customer
from ..deps import get_current_customer, get_db, get_settings
from ..ratelimit import SlidingWindowLimiter
from ..security import hash_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
_ip_limiter = SlidingWindowLimiter()
_addr_limiter = SlidingWindowLimiter()
IP_PER_MINUTE = 5
ADDRESS_PER_HOUR = 3
NOT_SET_UP = "Email is not set up on this service, so we cannot send messages yet. Contact support."
SAME_ANSWER = "If an account exists for that address, we have sent a message with a link. It can take a few minutes."


class ResetRequest(BaseModel):
    email: EmailStr


class ResetConfirm(BaseModel):
    token: str = Field(min_length=20, max_length=100)
    new_password: str = Field(min_length=8, max_length=72)


class TokenBody(BaseModel):
    token: str = Field(min_length=20, max_length=100)


class PrefsBody(BaseModel):
    opt_out_non_essential: bool


def _host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _send(settings: Settings, message) -> None:
    mailer = build_mailer(settings)
    if mailer is not None:
        await mailer.send(message)


@router.get("/email/available")
def email_available(settings: Settings = Depends(get_settings)) -> dict:
    """Public. Whether the service can send email at all, so the sign-in page can show or hide the reset link. Nothing else."""
    return {"available": configured(settings)}


async def _issue_reset_and_send(settings: Settings, address: str) -> None:
    """Runs AFTER the response, so what the request did (and how long it took) does not depend on whether the address has an account."""
    try:
        with session_scope() as db:
            customer = db.execute(select(Customer).where(Customer.email == address)).scalar_one_or_none()
            if customer is None or not customer.is_active:
                return
            raw = F.issue_token(db, customer.id, F.PURPOSE_RESET, timedelta(minutes=settings.email_reset_ttl_minutes))
            message = F.reset_message(settings, customer.email, raw) if raw is not None else None
        if message is not None:
            await _send(settings, message)
    except Exception:  # noqa: BLE001 - nothing here may reach the caller; no address in the log
        logging.getLogger(__name__).warning("a password reset message could not be prepared")


@router.post("/password-reset/request")
async def password_reset_request(body: ResetRequest, request: Request, background: BackgroundTasks, settings: Settings = Depends(get_settings)) -> dict:
    if not configured(settings):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
    allowed, retry = _ip_limiter.hit(f"reset:{_host(request)}", IP_PER_MINUTE)
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try again shortly", headers={"Retry-After": str(retry)})
    address = body.email.strip().lower()
    ok, _ = _addr_limiter.hit(f"reset-addr:{hashlib.sha256(address.encode()).hexdigest()}", ADDRESS_PER_HOUR, 3600.0)  # silent: the answer is the same either way
    if ok:
        background.add_task(_issue_reset_and_send, settings, address)
    return {"message": SAME_ANSWER}


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
def password_reset_confirm(body: ResetConfirm, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> None:
    allowed, retry = _ip_limiter.hit(f"reset-confirm:{_host(request)}", 10)
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try again shortly", headers={"Retry-After": str(retry)})
    customer_id = F.consume_token(db, body.token, F.PURPOSE_RESET)
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer is None or not customer.is_active:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link is not valid or has expired. Ask for a new one.")
    customer.password_hash = hash_password(body.new_password)
    db.commit()


@router.get("/email/status")
def email_status(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    state = db.get(F.CustomerEmailState, customer.id)
    return {"available": configured(settings), "verified": bool(state and state.verified_at), "opt_out_non_essential": bool(state and state.notices_opt_out)}


@router.post("/email/verify/request")
async def verify_request(request: Request, background: BackgroundTasks, customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    if not configured(settings):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_SET_UP)
    state = db.get(F.CustomerEmailState, customer.id)
    if state is not None and state.verified_at:
        return {"message": "This address is already confirmed."}
    raw = F.issue_token(db, customer.id, F.PURPOSE_VERIFY, timedelta(hours=settings.email_verify_ttl_hours))
    db.commit()
    if raw is None:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "We already sent several messages. Check your inbox and spam folder, or try again in an hour.")
    background.add_task(_send, settings, F.verify_message(settings, customer.email, raw))
    return {"message": "We have sent a message with a link. It can take a few minutes."}


@router.post("/email/verify/confirm")
def verify_confirm(body: TokenBody, request: Request, db: Session = Depends(get_db)) -> dict:
    allowed, retry = _ip_limiter.hit(f"verify-confirm:{_host(request)}", 20)
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, try again shortly", headers={"Retry-After": str(retry)})
    customer_id = F.consume_token(db, body.token, F.PURPOSE_VERIFY)
    if customer_id is None or db.get(Customer, customer_id) is None:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link is not valid or has expired. Ask for a new one.")
    F.mark_verified(db, customer_id)
    db.commit()
    return {"verified": True}


@router.post("/email/preferences")
def email_preferences(body: PrefsBody, customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)) -> dict:
    F.set_opt_out(db, customer.id, body.opt_out_non_essential)
    db.commit()
    return {"opt_out_non_essential": body.opt_out_non_essential}
