"""Admin dashboard routes (docs/security/admin_dashboard.md). Mounted ONLY when ADMIN_DASHBOARD_ENABLED is on (app.py); otherwise every path here is a plain 404. The panels are read only. The write routes at the
end (docs/safety/operator_settings.md) answer 403 unless ADMIN_ACTIONS_ENABLED is on, need a fresh authenticator code and a reason on every call, and write an admin_changes row; none of them can place, close or
modify a trade or an exchange order.

Who gets in: the normal sign-in first (the normal session cookie), then the customer's email must be in ADMIN_EMAILS, then an admin cookie from POST /login (a TOTP or recovery code). The cookie is renewed on each
request and lasts 15 minutes from the last one. No request header is read for any decision."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from ... import account_closure, admin_actions, admin_auth, admin_service, operator_settings
from ...config import Settings, load_settings
from ...db import session_scope
from ...models import AdminAudit, AdminChange, Customer, DataRequest, TradingAccount
from ..deps import get_current_customer, get_db, get_settings
from ..admin_schemas import (
    ActionDone,
    CompleteDeletionBody,
    DeletionDone,
    ChangesList,
    DaysBody,
    NoteBody,
    RequestsList,
    SettingReset,
    SettingSet,
    SettingsList,
    StepUp,
    AdminLoginOut,
    AdminLoginRequest,
    AdminMeOut,
    BillingPanelOut,
    BotsPanel,
    CustomerList,
    CustomersPanel,
    HealthPanel,
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


def _req_now(request: Request) -> datetime:
    """One reading of the clock per request, so the code check and the change record agree."""
    if getattr(request.state, "now", None) is None:
        request.state.now = _now()
    return request.state.now


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


@router.get("/billing", response_model=BillingPanelOut)
def billing(admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    return admin_service.cached(("billing",), lambda: admin_service.billing_panel(db, _now(), settings))


# ================================================================================================ writes (ADMIN_ACTIONS_ENABLED, step-up on every call)
_action_limiter = SlidingWindowLimiter()
ACTIONS_PER_MINUTE = 30


def _step_up(request: Request, db: Session, admin: Customer, settings: Settings, body: StepUp, label: str) -> None:
    """Gate every write: the switch, a valid reason, the rate limit, then a fresh authenticator code (the same checker as the sign-in: five wrong codes lock it, a used step is refused). The failure counters are
    committed before any refusal so they survive the exception."""
    if not settings.admin_actions_enabled:
        request.state.audit_detail = f"{label}: actions off"
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin actions are switched off (ADMIN_ACTIONS_ENABLED)")
    problem = admin_service.reason_problem(body.reason)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    allowed, retry = _action_limiter.hit(f"admin-action:{admin.id}", ACTIONS_PER_MINUTE)
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many actions, try again shortly", headers={"Retry-After": str(retry)})
    result = admin_auth.attempt_login(db, admin.id, body.code, encryption_key=settings.encryption_key, now=_req_now(request))
    db.commit()
    if result.outcome != admin_auth.R_OK:
        request.state.audit_detail = f"{label}: step-up {result.outcome}"
        if result.outcome == admin_auth.R_LOCKED:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many wrong codes, admin sign-in is locked for 15 minutes", headers={"Retry-After": str(result.retry_after)})
        if result.outcome == admin_auth.R_REPLAY:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "that code was already used; wait for the next code")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect code")
    request.state.audit_detail = label


def _run(db: Session, fn, *args, **kw):
    try:
        change = fn(db, *args, **kw)
    except (admin_actions.ActionError, operator_settings.SettingError) as exc:
        db.rollback()
        code = getattr(exc, "status_code", status.HTTP_422_UNPROCESSABLE_ENTITY)
        raise HTTPException(code, getattr(exc, "message", None) or str(exc)) from None
    db.commit()
    return change


def _done(change: AdminChange, restart: bool = False) -> ActionDone:
    return ActionDone(ok=True, action=change.action, old=change.old, new=change.new, restart_required=restart)


@router.get("/settings", response_model=SettingsList)
def settings_list(admin: Customer = Depends(require_admin), settings: Settings = Depends(get_settings)) -> dict:
    return {"settings": operator_settings.listing(settings), "actions_enabled": settings.admin_actions_enabled}


@router.get("/changes", response_model=ChangesList)
def changes(limit: int = Query(default=50, ge=1, le=200), admin: Customer = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.query(AdminChange).order_by(AdminChange.at.desc()).limit(limit).all()
    return {"changes": [{"at": r.at, "admin": r.admin_id[:8], "kind": r.kind, "action": r.action, "target": r.target[:8] if r.kind == "action" and r.target != "kill_switch" else r.target, "old": r.old, "new": r.new, "reason": r.reason} for r in rows]}


@router.get("/requests", response_model=RequestsList)
def pending_requests(admin: Customer = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.query(DataRequest).filter(DataRequest.status.in_(("open", "approved"))).order_by(DataRequest.requested_at).limit(100).all()
    prefixes = {a.customer_id: a.id[:8] for a in db.query(TradingAccount).filter(TradingAccount.customer_id.in_([r.customer_id for r in rows])).all()} if rows else {}
    return {"requests": [{"id": r.id, "kind": r.kind, "status": r.status, "requested_at": r.requested_at, "account_prefix": prefixes.get(r.customer_id)} for r in rows]}


@router.post("/settings/set", response_model=ActionDone)
def setting_set(body: SettingSet, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    _step_up(request, db, admin, settings, body, f"set {body.key[:30]}")
    ch = _run(db, lambda d: operator_settings.set_value(d, admin_id=admin.id, key=body.key, raw=body.value, reason=body.reason, base=load_settings(), now=_req_now(request)))
    return _done(ch, operator_settings.ALLOWLIST[body.key].restart_required)


@router.post("/settings/reset", response_model=ActionDone)
def setting_reset(body: SettingReset, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    _step_up(request, db, admin, settings, body, f"reset {body.key[:30]}")
    ch = _run(db, lambda d: operator_settings.reset_value(d, admin_id=admin.id, key=body.key, reason=body.reason, base=load_settings(), now=_req_now(request)))
    return _done(ch, operator_settings.ALLOWLIST[body.key].restart_required)


def _customer_action(name: str, fn, request, db, admin, settings, body, ref, *extra):
    _step_up(request, db, admin, settings, body, f"{name} {ref[:8]}")
    return _done(_run(db, lambda d: fn(d, admin.id, ref, *extra, body.reason, _req_now(request))))


@router.post("/customers/{ref}/pause", response_model=ActionDone)
def customer_pause(ref: str, body: StepUp, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    return _customer_action("pause", admin_actions.pause_bot, request, db, admin, settings, body, ref)


@router.post("/customers/{ref}/resume", response_model=ActionDone)
def customer_resume(ref: str, body: StepUp, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    return _customer_action("resume", admin_actions.resume_bot, request, db, admin, settings, body, ref)


@router.post("/customers/{ref}/suspend", response_model=ActionDone)
def customer_suspend(ref: str, body: StepUp, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    return _customer_action("suspend", admin_actions.suspend_customer, request, db, admin, settings, body, ref)


@router.post("/customers/{ref}/reinstate", response_model=ActionDone)
def customer_reinstate(ref: str, body: StepUp, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    return _customer_action("reinstate", admin_actions.reinstate_customer, request, db, admin, settings, body, ref)


@router.post("/customers/{ref}/subscription/extend", response_model=ActionDone)
def subscription_extend(ref: str, body: DaysBody, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    return _customer_action("extend", admin_actions.extend_subscription, request, db, admin, settings, body, ref, body.days)


@router.post("/customers/{ref}/subscription/end", response_model=ActionDone)
def subscription_end(ref: str, body: StepUp, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    return _customer_action("end_sub", admin_actions.end_subscription, request, db, admin, settings, body, ref)


@router.post("/customers/{ref}/note", response_model=ActionDone)
def customer_note(ref: str, body: NoteBody, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    _step_up(request, db, admin, settings, body, f"note {ref[:8]}")
    return _done(_run(db, lambda d: admin_actions.add_note(d, admin.id, ref, body.note, body.reason, _req_now(request))))


@router.post("/requests/{request_id}/approve", response_model=ActionDone)
def request_approve(request_id: str, body: StepUp, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    _step_up(request, db, admin, settings, body, f"approve {request_id[:8]}")
    return _done(_run(db, lambda d: admin_actions.decide_request(d, admin.id, request_id, True, body.reason, _req_now(request))))


@router.post("/requests/{request_id}/refuse", response_model=ActionDone)
def request_refuse(request_id: str, body: StepUp, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    _step_up(request, db, admin, settings, body, f"refuse {request_id[:8]}")
    return _done(_run(db, lambda d: admin_actions.decide_request(d, admin.id, request_id, False, body.reason, _req_now(request))))


@router.post("/kill-switch/engage", response_model=ActionDone)
def kill_engage(body: StepUp, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    _step_up(request, db, admin, settings, body, "kill engage")
    return _done(_run(db, lambda d: admin_actions.kill_flag(d, admin.id, True, body.reason, _req_now(request))))


@router.post("/kill-switch/release", response_model=ActionDone)
def kill_release(body: StepUp, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> ActionDone:
    _step_up(request, db, admin, settings, body, "kill release")
    return _done(_run(db, lambda d: admin_actions.kill_flag(d, admin.id, False, body.reason, _req_now(request))))


@router.post("/requests/{request_id}/complete-deletion", response_model=DeletionDone)
def request_complete_deletion(request_id: str, body: CompleteDeletionBody, request: Request, admin: Customer = Depends(require_admin), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> DeletionDone:
    """Erase an approved deletion request's customer (docs/safety/statements_and_closure.md). Needs ACCOUNT_DELETION_ENABLED on top of the admin actions switch, the exact confirmation text and a fresh code."""
    if not settings.account_deletion_enabled:
        request.state.audit_detail = "complete-deletion: switched off"
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account deletion is switched off (ACCOUNT_DELETION_ENABLED)")
    _step_up(request, db, admin, settings, body, f"delete {request_id[:8]}")
    try:
        result = account_closure.complete_deletion(db, admin.id, request_id, body.reason, body.confirm, admin_emails=settings.admin_emails, now=_req_now(request))
    except account_closure.ClosureError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.message) from None
    db.commit()
    return DeletionDone(ok=True, **result)
