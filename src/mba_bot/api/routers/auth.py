from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import Settings
from ...models import Customer
from ..deps import get_current_customer, get_db, get_settings
from ..schemas import CustomerOut, LoginRequest, SignupRequest
from ..security import SESSION_COOKIE_NAME, create_session_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

def _set_session_cookie(response: Response, *, customer_id: str, settings: Settings) -> None:
    if not settings.jwt_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "JWT_SECRET is not configured")
    token = create_session_token(
        customer_id=customer_id,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_minutes=settings.jwt_expires_minutes,
    )
    # Frontend (Vercel) and backend (this API) live on different domains,
    # so every request between them is cross-site as far as the browser is
    # concerned. A SameSite=Lax cookie is never sent on a cross-site
    # fetch()/XHR - only on a top-level link navigation - so Lax silently
    # drops the session on every API call from the deployed frontend.
    # SameSite=None is the only setting that works here, and browsers
    # require Secure whenever SameSite=None is used, so that's hardcoded
    # rather than left to settings.cookie_secure.
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.jwt_expires_minutes * 60,
        httponly=True,
        samesite="none",
        secure=True,
        path="/",
    )


@router.post("/signup", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def signup(
    body: SignupRequest, response: Response, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> Customer:
    customer = Customer(email=body.email.lower(), password_hash=hash_password(body.password))
    db.add(customer)
    try:
        db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists")

    _set_session_cookie(response, customer_id=customer.id, settings=settings)
    return customer


@router.post("/login", response_model=CustomerOut)
def login(
    body: LoginRequest, response: Response, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> Customer:
    customer = db.query(Customer).filter(Customer.email == body.email.lower()).first()
    if customer is None or not verify_password(body.password, customer.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect email or password")

    _set_session_cookie(response, customer_id=customer.id, settings=settings)
    return customer


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    # Attributes must match how the cookie was set, or the browser treats
    # this as a different cookie and leaves the real session cookie in place.
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite="none", secure=True)


@router.get("/me", response_model=CustomerOut)
def me(customer: Customer = Depends(get_current_customer)) -> Customer:
    return customer
