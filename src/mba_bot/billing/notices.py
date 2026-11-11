"""Customer-facing billing notices (designs/03, start of Stage 3). Runs ONLY while SUBSCRIPTION_BILLING_ENABLED is on.

Why this exists: Paystack does NOT retry a failed renewal (its written answer, 11 Nov). It marks the invoice failed, moves
the subscription to `attention` and e-mails the customer a manual retry link. So we also tell the customer ourselves:

  renewal_failed   a Telegram message and an in-app banner, once per failed period, saying access continues for the grace
                   period and where to pay. The payment link is the dashboard: its banner button calls
                   POST /api/billing/subscription/initialize again (allowed while the subscription is in grace or expired).
  card_expiring    Paystack's subscription.expiring_cards event: a reminder to update the card.
  period_end_soon  3 days before the period end, for subscriptions that are NOT paid by card renewal (provider other than
                   paystack, for example a manual grant or, later, crypto). Card subscriptions renew by themselves.
  cancelled        a banner only (derived, nothing stored): cancelling stops the next charge and access continues to the
                   end of the paid month.

Every text is a fixed string: no provider text, no e-mail, no amount that was not computed here. A notice is stored once per
(customer, kind, period) so a redelivered event or a repeated job run sends nothing twice. The Telegram send never raises
and never holds a database write lock (it runs after the commit).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import session_scope
from ..models import BillingNotice, Customer, Subscription, TradingAccount
from ..notifications.telegram import TelegramNotifier
from .entitlement import EntitlementState, _aware, entitlement_state, grace_end

logger = logging.getLogger(__name__)

KIND_RENEWAL_FAILED = "renewal_failed"
KIND_CARD_EXPIRING = "card_expiring"
KIND_PERIOD_END = "period_end_soon"
KIND_CANCELLED = "cancelled"
REMINDER_DAYS = 3
CARD_PROVIDER = "paystack"

# The cancel wording (owner request, 11 Nov): Paystack's own cancel page says access is lost; ours must not.
CANCEL_NOTE = "Cancelling stops the next charge. Your access continues until the end of the month you have already paid for."


def _date(value: datetime) -> str:
    return f"{value.day} {value.strftime('%b %Y')}"


def dashboard_link(settings: Settings) -> str:
    return settings.frontend_origin.rstrip("/") + "/#/dashboard"


def grace_phrase(settings: Settings) -> str:
    return f"{max(0, int(settings.subscription_grace_days))}-day grace period"


def message_for(kind: str, settings: Settings, sub: Subscription, *, link: bool = True) -> str:
    end = _aware(sub.current_period_end)
    if end is None:
        return ""
    grace = grace_phrase(settings)
    if kind == KIND_RENEWAL_FAILED:
        gend = grace_end(sub, settings.subscription_grace_days)
        tail = f"pay now from your dashboard: {dashboard_link(settings)}" if link else "pay now."
        return (f"Your Metabridge subscription renewal payment failed. Your access continues for the {grace}, until {_date(gend)}. "
                f"To keep your access, {tail}")
    if kind == KIND_PERIOD_END:
        tail = f" Your dashboard: {dashboard_link(settings)}" if link else ""
        return (f"Your Metabridge subscription ends on {_date(end)}. Renew before then to avoid a break. "
                f"If you do not, your access continues for the {grace} after that date.{tail}")
    if kind == KIND_CARD_EXPIRING:
        return (f"The card used for your Metabridge subscription is about to expire. Please update it before your next payment on "
                f"{_date(end)} so the renewal can go through. If a renewal fails, your access continues for the {grace}. "
                f"Contact support if you need help.")
    if kind == KIND_CANCELLED:
        return f"Your subscription will not renew. {CANCEL_NOTE} Your access continues until {_date(end)}."
    return ""


def create_notice(db: Session, customer_id: str, kind: str, period_key: str, expires_at: datetime) -> bool:
    """True if this notice is new (so it should be sent now); False if it was already raised for this period."""
    try:
        with db.begin_nested():
            db.add(BillingNotice(customer_id=customer_id, kind=kind, period_key=period_key[:40], expires_at=expires_at))
            db.flush()
    except IntegrityError:
        return False
    return True


def active_notice(db: Session, customer: Customer, settings: Settings, now: datetime) -> dict | None:
    """What the in-app banner shows right now, or None. Read-only. Priority: renewal failed, card expiring, ends soon, cancelled."""
    if not settings.subscription_billing_enabled:
        return None
    sub = customer.subscription
    end = _aware(sub.current_period_end) if sub is not None else None
    if sub is None or end is None:
        return None
    state = entitlement_state(customer, settings=settings, now=now)
    stored = {n.kind for n in db.query(BillingNotice).filter_by(customer_id=customer.id, period_key=end.isoformat()).all()
              if _aware(n.expires_at) > now}
    can_pay = state in (EntitlementState.GRACE, EntitlementState.EXPIRED)  # the same rule the initialize route applies
    if KIND_RENEWAL_FAILED in stored and state in (EntitlementState.GRACE, EntitlementState.ACTIVE):
        return {"kind": KIND_RENEWAL_FAILED, "message": message_for(KIND_RENEWAL_FAILED, settings, sub, link=False),
                "action": "pay" if can_pay else None}
    if KIND_CARD_EXPIRING in stored and state == EntitlementState.ACTIVE:
        return {"kind": KIND_CARD_EXPIRING, "message": message_for(KIND_CARD_EXPIRING, settings, sub), "action": None}
    if (state == EntitlementState.ACTIVE and sub.provider != CARD_PROVIDER and sub.status == "active"
            and now < end <= now + timedelta(days=REMINDER_DAYS)):
        return {"kind": KIND_PERIOD_END, "message": message_for(KIND_PERIOD_END, settings, sub, link=False), "action": None}
    if state == EntitlementState.CANCELLED:
        return {"kind": KIND_CANCELLED, "message": message_for(KIND_CANCELLED, settings, sub), "action": None}
    return None


# ----------------------------------------------------------------- delivery (never raises, never inside a write transaction)

def telegram_target(db: Session, customer_id: str) -> str | None:
    account = db.query(TradingAccount).filter_by(customer_id=customer_id).first()
    if account is None or account.notification_channel != "telegram" or not account.notification_target:
        return None
    return account.notification_target


async def send_to_customer(settings: Settings, target: str | None, text: str) -> bool:
    if not (target and text and settings.telegram_bot_token):
        return False
    try:
        await TelegramNotifier(settings.telegram_bot_token).send(target, text)
        return True
    except Exception:  # noqa: BLE001 - a reminder must never break billing or the scheduler
        logger.warning("a billing message to a customer could not be delivered")
        return False


async def deliver_messages(db: Session, settings: Settings, messages: list[tuple[str, str]]) -> None:
    """Called AFTER the commit, so no write lock is held while Telegram is awaited."""
    for customer_id, text in messages:
        await send_to_customer(settings, telegram_target(db, customer_id), text)


# ----------------------------------------------------------------- the 3-days-before reminder (non-card subscriptions)

def due_period_end_reminders(db: Session, settings: Settings, now: datetime) -> list[tuple[str, str]]:
    """Raise a period_end_soon notice (once per period) for every active subscription not paid by card renewal whose period
    ends within REMINDER_DAYS. Returns the (customer id, text) pairs to send once the transaction is committed."""
    out: list[tuple[str, str]] = []
    rows = (db.query(Subscription).filter(Subscription.provider.isnot(None), Subscription.provider != CARD_PROVIDER,
                                          Subscription.status == "active").all())
    for sub in rows:
        end = _aware(sub.current_period_end)
        if end is None or not (now < end <= now + timedelta(days=REMINDER_DAYS)):
            continue
        if create_notice(db, sub.customer_id, KIND_PERIOD_END, end.isoformat(), end):
            out.append((sub.customer_id, message_for(KIND_PERIOD_END, settings, sub)))
    return out


async def run_reminders_once(settings: Settings, now: datetime | None = None) -> int:
    """One pass of the reminder job. Safe to run as often as you like: a period is only ever notified once."""
    if not settings.subscription_billing_enabled:
        return 0
    now = now or datetime.now(timezone.utc)
    with session_scope() as db:
        due = due_period_end_reminders(db, settings, now)
        targets = [(telegram_target(db, cid), text) for cid, text in due]
    for target, text in targets:  # after the commit
        await send_to_customer(settings, target, text)
    return len(targets)
