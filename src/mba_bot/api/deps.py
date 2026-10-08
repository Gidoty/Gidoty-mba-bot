"""Shared FastAPI dependencies: a DB session per request, and the
customer resolved from the session cookie."""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..config import Settings, load_settings
from ..db import session_scope
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
