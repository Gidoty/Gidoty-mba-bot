"""Shared FastAPI dependencies: a DB session per request, and the
customer resolved from the session cookie."""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..config import Settings, load_settings
from ..db import session_scope
from .. import pair_catalog
from ..legal import has_current_terms_and_privacy
from ..models import Customer
from .security import SESSION_COOKIE_NAME, TokenError, read_customer_id


def get_settings() -> Settings:
    return load_settings()


def get_db() -> Iterator[Session]:
    with session_scope() as session:
        yield session


def get_current_customer(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> Customer:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token or not settings.jwt_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    try:
        customer_id = read_customer_id(token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm)
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated") from exc

    customer = db.get(Customer, customer_id)
    if customer is None or not customer.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    return customer


def require_legal_acceptance(
    customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)
) -> Customer:
    """Gate for every /api/account/* route: a customer who hasn't
    accepted the CURRENT Terms of Service and Privacy Policy can't reach
    any trading functionality, however they got here - including a
    customer who signed up before these documents existed and therefore
    has zero acceptance rows on file. Nothing backfills that; they have
    to actually accept it once, via POST /api/auth/accept-legal. The
    distinct "ACCEPTANCE_REQUIRED" prefix lets the frontend tell this
    apart from an ordinary 403 and show the consent screen instead of a
    generic error."""
    if not has_current_terms_and_privacy(db, customer_id=customer.id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "ACCEPTANCE_REQUIRED: accept the current Terms of Service and Privacy Policy before continuing",
        )
    return customer


async def get_pair_facts(settings: Settings = Depends(get_settings)) -> dict | None:
    """Current exchange facts for the Futures pair catalog, or None. With the
    expansion OFF this is None immediately and nothing is ever fetched - the
    existing behaviour. Never raises: an unreadable catalog just means no
    availability checks (see pair_catalog)."""
    if not settings.futures_pair_expansion:
        return None
    return await pair_catalog.futures_pair_facts()
