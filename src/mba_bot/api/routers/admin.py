"""Admin dashboard routes (docs/security/admin_dashboard.md). Mounted ONLY when ADMIN_DASHBOARD_ENABLED is on (app.py); otherwise every path here is a plain 404. Read-only: nothing here starts, stops,
changes or deletes anything. The only writes are the admin's own audit rows and the sign-in counters.

Who gets in: the normal sign-in first (the normal session cookie), then the customer's email must be in ADMIN_EMAILS, then an admin cookie from POST /login (a TOTP or recovery code). The cookie is renewed on each
request and lasts 15 minutes from the last one. No request header is read for any decision."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from ... import admin_auth, admin_service
from ...config import Settings
from ...db import session_scope
from ...models import AdminAudit, Customer
from ..deps import get_current_customer, get_db, get_settings
from ..admin_schemas import (
    AdminLoginOut,
    AdminLoginRequest,
    AdminMeOut,
    BillingPanelOut,
    BotsPanel,
    CustomerList,
    CustomersPanel,
    HealthPanel,
    OpenTradesPanel,
    ResultsPanel,
    RevealOut,
    RevealRequest,
    SkipsPanel,
)
from ..ratelimit import SlidingWindowLimiter


class AuditedRoute(APIRoute):
    """Writes one audit row for every request made by a customer who is in ADMIN_EMAILS (signed in normally): the action word, the path and the HTTP status, plus a short detail the handler set. Requests from
    anyone else are refused before they have an identity and write nothing. The row is written in its own transaction so a refused or failed request is recorded too."""

    def get_route_handler(self):
        original = super().get_route_handler()
        action = self.name[:24]

        async def handler(request: Request):
            code, response, pending = 500, None, None
            try:
                response = await original(request)
                code = response.status_code
            except HTTPException as exc:
                code, pending = exc.status_code, exc
            except Exception as exc:  # noqa: BLE001 - request validation errors and anything unexpected
                code = getattr(exc, "status_code", None) or (422 if type(exc).__name__ == "RequestValidationError" else 500)
                pending = exc
            admin_id = getattr(request.state, "admin_id", None)
            if admin_id and not getattr(request.state, "audit_written", False):
                try:
                    with session_scope() as s:
                        s.add(AdminAudit(customer_id=admin_id, action=action, path=request.url.path[:80], status=int(code), detail=(getattr(request.state, "audit_detail", None) or None)))
                except Exception:  # noqa: BLE001
                    # Fail closed: an admin request that cannot be recorded gets no data back.
                    logger.error("admin audit row could not be written; the request was refused")
                    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "audit log unavailable") from None
            if pending is not None:
                raise pending
            return response

        return handler


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"], route_class=AuditedRoute)

_login_limiter = SlidingWindowLimiter()
LOGIN_PER_MINUTE = 10


def _cookie(response: Response, customer_id: str, settings: Settings) -> None:
    token = admin_auth.create_admin_token(customer_id=customer_id, jwt_secret=settings.jwt_secret, algorithm=settings.jwt_algorithm)
    response.set_cookie(admin_auth.ADMIN_COOKIE_NAME, token, max_age=admin_auth.SESSION_MINUTES * 60, httponly=True, samesite="none", secure=True, path="/api/admin")


def _require_admin_email(request: Request, customer: Customer, settings: Settings) -> None:
    if not admin_auth.is_admin_email(customer.email, settings.admin_emails):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    request.state.admin_id = customer.id


def require_admin(request: Request, response: Response, customer: Customer = Depends(get_current_customer), settings: Settings = Depends(get_settings)) -> Customer:
    """A normally signed-in customer whose email is in ADMIN_EMAILS AND who holds a valid, unexpired admin cookie for the same customer. Renews the cookie (15 minutes from now)."""
    _require_admin_email(request, customer, settings)
    owner = admin_auth.read_admin_token(request.cookies.get(admin_auth.ADMIN_COOKIE_NAME), jwt_secret=settings.jwt_secret, algorithm=settings.jwt_algorithm)
    if owner != customer.id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin sign-in required")
    _cookie(response, customer.id, settings)
    return customer


@router.post("/login", response_model=AdminLoginOut)
def admin_login(
    body: AdminLoginRequest, request: Request, response: Response, customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> AdminLoginOut:
    _require_admin_email(request, customer, settings)
    host = request.client.host if request.client else "unknown"  # the address uvicorn resolved; no header is read here
    allowed, retry = _login_limiter.hit(f"admin-login:{host}", LOGIN_PER_MINUTE)
    if not allowed:
        request.state.audit_detail = "rate_limited"
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts, try again shortly", headers={"Retry-After": str(retry)})
    result = admin_auth.attempt_login(db, customer.id, body.code, encryption_key=settings.encryption_key)
    request.state.audit_detail = result.outcome
    if result.outcome == admin_auth.R_OK:
        db.commit()  # release the write lock now: the audit row is written in its own transaction right after this handler returns
        _cookie(response, customer.id, settings)
        return AdminLoginOut(ok=True, expires_in_seconds=admin_auth.SESSION_MINUTES * 60)
    db.commit()  # keep the failure counter and the lock: the exception below would otherwise roll them back
    if result.outcome == admin_auth.R_LOCKED:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many wrong codes, admin sign-in is locked for 15 minutes", headers={"Retry-After": str(result.retry_after)})
    if result.outcome == admin_auth.R_NOT_ENROLLED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "two-factor sign-in is not set up for this account; the owner enrols it on the server")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect code")


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def admin_logout(response: Response) -> None:
    response.delete_cookie(admin_auth.ADMIN_COOKIE_NAME, path="/api/admin", samesite="none", secure=True)


@router.get("/me", response_model=AdminMeOut)
def admin_me(request: Request, customer: Customer = Depends(get_current_customer), settings: Settings = Depends(get_settings)) -> AdminMeOut:
    """For the screen: is this signed-in customer an admin, and do they hold an admin session? Nothing else."""
    if not admin_auth.is_admin_email(customer.email, settings.admin_emails):
        return AdminMeOut(is_admin=False, session=False)
    request.state.admin_id = customer.id
    owner = admin_auth.read_admin_token(request.cookies.get(admin_auth.ADMIN_COOKIE_NAME), jwt_secret=settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return AdminMeOut(is_admin=True, session=owner == customer.id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/customers/summary", response_model=CustomersPanel)
def customers_summary(admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    return admin_service.cached(("customers",), lambda: admin_service.customers_panel(db, _now(), settings))


@router.get("/bots", response_model=BotsPanel)
def bots(admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    return admin_service.cached(("bots",), lambda: admin_service.bots_panel(db, _now(), settings))


@router.get("/health", response_model=HealthPanel)
def health(admin: Customer = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    return admin_service.cached(("health",), lambda: admin_service.health_panel(db, _now(), commit=admin_service.deploy_commit()))


@router.get("/results", response_model=ResultsPanel)
def results(window_days: int = Query(default=30), admin: Customer = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    if window_days not in admin_service.WINDOWS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "window_days must be 7, 30 or 0 (all time)")
    return admin_service.cached(("results", window_days), lambda: admin_service.results_panel(db, _now(), window_days=window_days))


@router.get("/customers", response_model=CustomerList)
def customers(request: Request, page: int = Query(default=1, ge=1, le=100000), admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    request.state.audit_detail = f"page {page}"
    return admin_service.cached(("customers_page", page), lambda: admin_service.customer_list(db, _now(), settings, page=page))


@router.post("/customers/reveal", response_model=RevealOut)
def reveal(body: RevealRequest, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db)) -> RevealOut:
    """The full email of one customer, once: needs a reason (5 to 200 characters), is written to the audit log in the same transaction, and is limited to 20 a day per admin."""
    problem = admin_service.reason_problem(body.reason)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    now = _now()
    used = admin_service.reveals_today(db, admin.id, now)
    if used >= admin_service.REVEAL_PER_DAY:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"daily reveal limit reached ({admin_service.REVEAL_PER_DAY})")
    email = admin_service.email_of(db, body.ref)
    if email is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such customer")
    db.add(AdminAudit(customer_id=admin.id, action="reveal", path=request.url.path[:80], status=200, detail=f"{body.ref[:8]}: {body.reason.strip()}"[:200]))
    request.state.audit_written = True
    return RevealOut(email=email, reveals_left_today=admin_service.REVEAL_PER_DAY - used - 1)


@router.get("/skipped-pairs", response_model=SkipsPanel)
def skipped_pairs(admin: Customer = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    return admin_service.cached(("skips",), lambda: admin_service.skips_panel(db, _now()))


@router.get("/open-trades", response_model=OpenTradesPanel)
def open_trades(admin: Customer = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    return admin_service.cached(("open_trades",), lambda: admin_service.open_trades_panel(db, _now()))


@router.get("/billing", response_model=BillingPanelOut)
def billing(admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    return admin_service.cached(("billing",), lambda: admin_service.billing_panel(db, _now(), settings))
