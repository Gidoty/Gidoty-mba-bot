"""Per-customer admin actions (docs/safety/operator_settings.md). Each function changes ONE thing in the database and adds one AdminChange row (who, when, old, new, reason) in the same transaction; the caller commits.

What none of them can do: place, close or modify a trade or an exchange order, call an exchange, a payment provider or the customer. Pausing a bot or suspending a customer only stops NEW entries (the scheduler
still manages every open trade: db.list_active_accounts lists an inactive account that has an open trade). The kill-switch action only sets the existing kill flag through kill_switch.set_state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import kill_switch, strategy_availability
from .multi_trade import account_strategies
from .db import list_open_trades
from .models import AdminChange, Customer, CustomerNote, DataRequest, Subscription, TradingAccount

MAX_EXTEND_DAYS = 366


class ActionError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status_code, message


def clean_reason(reason) -> str:
    text = " ".join(str(reason or "").split())
    if not (5 <= len(text) <= 200):
        raise ActionError(422, "Give a reason of 5 to 200 characters.")
    return text


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(v: datetime | None) -> datetime | None:
    return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc))


def account_and_customer(db: Session, ref: str) -> tuple[TradingAccount, Customer]:
    row = db.execute(select(TradingAccount, Customer).join(Customer, Customer.id == TradingAccount.customer_id).where(TradingAccount.id == ref)).first()
    if row is None:
        raise ActionError(404, "no such customer")
    return row[0], row[1]


def _log(db: Session, admin_id: str, action: str, target: str, old, new, reason: str, now: datetime) -> AdminChange:
    ch = AdminChange(at=now, admin_id=admin_id, kind="action", action=action, target=target[:80], old=None if old is None else str(old)[:120], new=None if new is None else str(new)[:120], reason=reason)
    db.add(ch)
    return ch


def pause_bot(db: Session, admin_id: str, ref: str, reason: str, now: datetime | None = None) -> AdminChange:
    reason, now = clean_reason(reason), now or _now()
    acct, _ = account_and_customer(db, ref)
    if not acct.is_active:
        raise ActionError(409, "the bot is already paused")
    acct.is_active = False
    return _log(db, admin_id, "pause_bot", acct.customer_id, "running", "paused", reason, now)


def resume_bot(db: Session, admin_id: str, ref: str, reason: str, now: datetime | None = None) -> AdminChange:
    reason, now = clean_reason(reason), now or _now()
    acct, cust = account_and_customer(db, ref)
    if not cust.is_active:
        raise ActionError(409, "the account is suspended; reinstate it first")
    if acct.is_active:
        raise ActionError(409, "the bot is not paused")
    acct.is_active = True
    return _log(db, admin_id, "resume_bot", acct.customer_id, "paused", "running", reason, now)


def suspend_customer(db: Session, admin_id: str, ref: str, reason: str, now: datetime | None = None) -> AdminChange:
    reason, now = clean_reason(reason), now or _now()
    acct, cust = account_and_customer(db, ref)
    if not cust.is_active:
        raise ActionError(409, "the account is already suspended")
    old = f"active,bot {'running' if acct.is_active else 'paused'}"
    cust.is_active = False
    acct.is_active = False
    return _log(db, admin_id, "suspend", cust.id, old, "suspended,bot paused", reason, now)


def reinstate_customer(db: Session, admin_id: str, ref: str, reason: str, now: datetime | None = None) -> AdminChange:
    """Lets the customer sign in again. The bot stays paused: resume it as a separate action."""
    reason, now = clean_reason(reason), now or _now()
    acct, cust = account_and_customer(db, ref)
    if cust.is_active:
        raise ActionError(409, "the account is not suspended")
    cust.is_active = True
    return _log(db, admin_id, "reinstate", cust.id, "suspended", "active,bot paused", reason, now)


def extend_subscription(db: Session, admin_id: str, ref: str, days: int, reason: str, now: datetime | None = None) -> AdminChange:
    """The manual extension (Stage 3): the period end moves to max(now, current end) plus `days`, the status becomes active. A customer with no subscription row gets one with provider `manual`. This changes the
    database only: it does not touch Paystack or NOWPayments, so a provider-side subscription is not cancelled or charged by it."""
    reason, now = clean_reason(reason), now or _now()
    if isinstance(days, bool) or not isinstance(days, int) or not (1 <= days <= MAX_EXTEND_DAYS):
        raise ActionError(422, f"Extend by 1 to {MAX_EXTEND_DAYS} days.")
    _, cust = account_and_customer(db, ref)
    sub = db.execute(select(Subscription).where(Subscription.customer_id == cust.id)).scalar_one_or_none()
    if sub is None:
        sub = Subscription(customer_id=cust.id, status="active", current_period_start=now, current_period_end=now + timedelta(days=days), provider="manual")
        db.add(sub)
        return _log(db, admin_id, "extend", cust.id, "no subscription", f"until {sub.current_period_end:%Y-%m-%d}", reason, now)
    end = _aware(sub.current_period_end)
    old = f"{sub.status} until {end:%Y-%m-%d}" if end else sub.status
    start = max(now, end) if end else now
    sub.current_period_end = start + timedelta(days=days)
    sub.current_period_start = sub.current_period_start or now
    sub.status = "active"
    sub.grace_until = None
    return _log(db, admin_id, "extend", cust.id, old, f"until {sub.current_period_end:%Y-%m-%d}", reason, now)


def end_subscription(db: Session, admin_id: str, ref: str, reason: str, now: datetime | None = None) -> AdminChange:
    """Ends access now: status expired, period end now, no grace. Database only (a provider-side subscription is not cancelled by this)."""
    reason, now = clean_reason(reason), now or _now()
    _, cust = account_and_customer(db, ref)
    sub = db.execute(select(Subscription).where(Subscription.customer_id == cust.id)).scalar_one_or_none()
    if sub is None:
        raise ActionError(404, "this customer has no subscription row")
    end = _aware(sub.current_period_end)
    old = f"{sub.status} until {end:%Y-%m-%d}" if end else sub.status
    sub.status, sub.current_period_end = "expired", now
    return _log(db, admin_id, "end_subscription", cust.id, old, "expired now", reason, now)


def decide_request(db: Session, admin_id: str, request_id: str, approve: bool, reason: str, now: datetime | None = None) -> AdminChange:
    """A pending data request (export or deletion): approve marks it `approved` (the operator then completes it; nothing is deleted here), refuse marks it `declined`."""
    reason, now = clean_reason(reason), now or _now()
    row = db.get(DataRequest, request_id)
    if row is None:
        raise ActionError(404, "no such request")
    if row.status != "open":
        raise ActionError(409, f"the request is already {row.status}")
    row.status = "approved" if approve else "declined"
    row.handled_at = now
    return _log(db, admin_id, "approve_request" if approve else "refuse_request", request_id, f"{row.kind} open", row.status, reason, now)


def add_note(db: Session, admin_id: str, ref: str, note: str, reason: str, now: datetime | None = None) -> AdminChange:
    reason, now = clean_reason(reason), now or _now()
    text = " ".join(str(note or "").split())
    if not (1 <= len(text) <= 500):
        raise ActionError(422, "A note is 1 to 500 characters.")
    _, cust = account_and_customer(db, ref)
    db.add(CustomerNote(customer_id=cust.id, admin_id=admin_id, at=now, note=text))
    return _log(db, admin_id, "note", cust.id, None, f"{len(text)} characters", reason, now)


def kill_flag(db: Session, admin_id: str, engage: bool, reason: str, now: datetime | None = None) -> AdminChange:
    """Sets the existing kill flag and nothing else (kill_switch.set_state: one row, one audit event). Engaged: no NEW live entries on any account; open trades keep being managed. The caller must have
    committed any pending write first (SQLite takes one writer at a time)."""
    reason, now = clean_reason(reason), now or _now()
    engine = db.get_bind()
    prev = kill_switch.read_row(engine)
    result = kill_switch.set_state(engine, engaged=engage, actor="admin dashboard", reason=reason, now=now)
    if not result.ok:
        raise ActionError(503, "the kill flag could not be written; nothing was changed")
    return _log(db, admin_id, "kill_engage" if engage else "kill_release", "kill_switch", getattr(prev, "state", None), engage, reason, now)


def set_strategies(db: Session, admin_id: str, ref: str, wanted, reason: str, now: datetime | None = None) -> AdminChange:
    """The admin override of an account's strategies (`extra_strategies`): any known strategies, whatever is available to customers. It only changes what the account is opted in to; whether a strategy trades live is
    still decided by the operator's own live switches. Refused while a trade of the account is open (an open trade is managed by its own strategy and must not lose it)."""
    reason, now = clean_reason(reason), now or _now()
    acct, cust = account_and_customer(db, ref)
    try:
        value = strategy_availability.admin_value(wanted)
    except strategy_availability.ChoiceError as exc:
        raise ActionError(422, str(exc)) from None
    if list_open_trades(db, acct.id):
        raise ActionError(409, "a trade of this account is open; change this after it has closed")
    old = ",".join(sorted(account_strategies(acct))) or "ema only"
    acct.extra_strategies = value
    return _log(db, admin_id, "set_strategies", cust.id, old, (value or "ema only")[:120], reason, now)
