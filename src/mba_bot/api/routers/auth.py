from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import Settings
from ...legal import (
    PRIVACY_VERSION,
    TERMS_VERSION,
    has_current_acceptance,
    has_current_terms_and_privacy,
    record_acceptance,
)
from ...models import Customer
from ..deps import get_current_customer, get_db, get_settings
from ..ratelimit import LoginThrottle
from ..schemas import AcceptLegalRequest, CustomerOut, LoginRequest, SignupRequest
from ..security import SESSION_COOKIE_NAME, create_session_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

_throttles: dict[tuple[int, int], LoginThrottle] = {}


def _login_throttle(settings: Settings) -> LoginThrottle | None:
    """One throttle per process and per (limit, lockout) setting. None while LOGIN_THROTTLE_ENABLED is off."""
    if not settings.login_throttle_enabled:
        return None
    key = (settings.login_max_failures, settings.login_lockout_seconds)
    if key not in _throttles:
        _throttles[key] = LoginThrottle(max_failures=key[0], lockout_seconds=key[1])
    return _throttles[key]


def _to_customer_out(customer: Customer, db: Session) -> CustomerOut:
    return CustomerOut(
        id=customer.id,
        email=customer.email,
        created_at=customer.created_at,
        has_paid=customer.has_paid,
        legal_acceptance_current=has_current_terms_and_privacy(db, customer_id=customer.id),
    )

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
) -> CustomerOut:
    # body.accept_terms / body.acknowledge_privacy are already guaranteed
    # True here - SignupRequest's validators reject a False/missing value
    # with a 422 before this function body ever runs, so there is no
    # direct-API path that creates a customer without both.
    customer = Customer(email=body.email.lower(), password_hash=hash_password(body.password))
    db.add(customer)
    try:
        db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists")

    record_acceptance(db, customer_id=customer.id, document_type="terms", version=TERMS_VERSION)
    record_acceptance(db, customer_id=customer.id, document_type="privacy", version=PRIVACY_VERSION)

    _set_session_cookie(response, customer_id=customer.id, settings=settings)
    return _to_customer_out(customer, db)


@router.post("/login", response_model=CustomerOut)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CustomerOut:
    throttle = _login_throttle(settings)
    ip = request.client.host if request.client else "unknown"
    if throttle is not None:
        wait = throttle.blocked(ip, body.email)
        if wait:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many failed sign-in attempts, try again later", headers={"Retry-After": str(wait)})
    customer = db.query(Customer).filter(Customer.email == body.email.lower()).first()
    if customer is None or not verify_password(body.password, customer.password_hash):
        if throttle is not None:
            throttle.record_failure(ip, body.email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect email or password")
    if throttle is not None:
        throttle.record_success(ip, body.email)

    _set_session_cookie(response, customer_id=customer.id, settings=settings)
    return _to_customer_out(customer, db)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    # Attributes must match how the cookie was set, or the browser treats
    # this as a different cookie and leaves the real session cookie in place.
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite="none", secure=True)


@router.get("/me", response_model=CustomerOut)
def me(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)) -> CustomerOut:
    return _to_customer_out(customer, db)


@router.post("/accept-legal", response_model=CustomerOut)
def accept_legal(
    body: AcceptLegalRequest, customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)
) -> CustomerOut:
    """For a customer who is already logged in but whose on-file
    acceptance predates the current Terms/Privacy version - most
    notably every customer who existed before this feature shipped.
    Idempotent: calling it again once already current is a no-op rather
    than a duplicate-row error, same spirit as mark_customer_paid.py
    being a no-op on an already-paid customer."""
    if not has_current_acceptance(db, customer_id=customer.id, document_type="terms", version=TERMS_VERSION):
        record_acceptance(db, customer_id=customer.id, document_type="terms", version=TERMS_VERSION)
    if not has_current_acceptance(db, customer_id=customer.id, document_type="privacy", version=PRIVACY_VERSION):
        record_acceptance(db, customer_id=customer.id, document_type="privacy", version=PRIVACY_VERSION)
    return _to_customer_out(customer, db)
