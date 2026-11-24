"""Email verification, password reset and customer notices (docs/safety/email_and_recovery.md). Tokens are random, single-use, expiring and stored hashed; nothing here reveals whether an address has an account;
consent (the legal acceptance rows) is never read or written here.

Essential mail (verification, password reset, a notice that affects the service) is always sent. Non-essential mail (reminders, product notices) is not sent to a customer who opted out. Every send is best effort: a
failure is logged by class and never breaks the caller."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .config import Settings
from .mailer import Message, build_mailer
from .models import Customer, CustomerEmailState, EmailToken

logger = logging.getLogger(__name__)
PURPOSE_VERIFY, PURPOSE_RESET = "verify", "reset"
MAX_PER_HOUR = 3  # tokens per customer and purpose per hour
KEEP_DAYS = 7


def _aware(v: datetime | None) -> datetime | None:
    return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc))


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(db: Session, customer_id: str, purpose: str, ttl: timedelta, now: datetime | None = None) -> str | None:
    """A fresh token for the customer, or None when the hourly limit is reached. Earlier unused tokens of the same purpose stop working (only the newest link is valid). Old rows are deleted."""
    now = now or datetime.now(timezone.utc)
    db.query(EmailToken).filter(EmailToken.created_at < now - timedelta(days=KEEP_DAYS)).delete(synchronize_session=False)
    recent = db.execute(select(func.count()).select_from(EmailToken).where(EmailToken.customer_id == customer_id, EmailToken.purpose == purpose, EmailToken.created_at > now - timedelta(hours=1))).scalar_one()
    if recent >= MAX_PER_HOUR:
        return None
    db.execute(update(EmailToken).where(EmailToken.customer_id == customer_id, EmailToken.purpose == purpose, EmailToken.used_at.is_(None)).values(used_at=now))
    raw = secrets.token_urlsafe(32)
    db.add(EmailToken(customer_id=customer_id, purpose=purpose, token_hash=hash_token(raw), created_at=now, expires_at=now + ttl))
    db.flush()
    return raw


def consume_token(db: Session, raw: str, purpose: str, now: datetime | None = None) -> str | None:
    """The customer id the token belongs to, and the token is used up; or None for an unknown, used, expired or wrong-purpose token. The use is one atomic UPDATE, so two requests cannot both succeed."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(raw, str) or not (20 <= len(raw) <= 100):
        return None
    row = db.execute(select(EmailToken).where(EmailToken.token_hash == hash_token(raw))).scalar_one_or_none()
    if row is None or row.purpose != purpose or row.used_at is not None or _aware(row.expires_at) <= now:
        return None
    moved = db.execute(update(EmailToken).where(EmailToken.id == row.id, EmailToken.used_at.is_(None)).values(used_at=now)).rowcount
    return row.customer_id if moved == 1 else None


def state_of(db: Session, customer_id: str) -> CustomerEmailState:
    row = db.get(CustomerEmailState, customer_id)
    if row is None:
        row = CustomerEmailState(customer_id=customer_id, notices_opt_out=False)
        db.add(row)
        db.flush()
    return row


def mark_verified(db: Session, customer_id: str, now: datetime | None = None) -> None:
    row = state_of(db, customer_id)
    row.verified_at = row.verified_at or (now or datetime.now(timezone.utc))
    row.updated_at = now or datetime.now(timezone.utc)


def set_opt_out(db: Session, customer_id: str, opt_out: bool, now: datetime | None = None) -> None:
    row = state_of(db, customer_id)
    row.notices_opt_out = bool(opt_out)
    row.updated_at = now or datetime.now(timezone.utc)


def link(settings: Settings, kind: str, raw: str) -> str:
    """The frontend link. The token is in the URL fragment, so it is never sent to a server as part of a request line or written to an access log."""
    return f"{settings.frontend_origin.rstrip('/')}/#/{kind}/{raw}"


def reset_message(settings: Settings, to: str, raw: str) -> Message:
    return Message(to, "Reset your MBA Bot password", f"Someone asked to reset the password of your MBA Bot account.\n\nOpen this link to choose a new password (it works once and expires in {settings.email_reset_ttl_minutes} minutes):\n"
                   f"{link(settings, 'reset-password', raw)}\n\nIf this was not you, ignore this email: your password has not changed.")


def verify_message(settings: Settings, to: str, raw: str) -> Message:
    return Message(to, "Confirm your email address for MBA Bot", f"Open this link to confirm this email address (it works once and expires in {settings.email_verify_ttl_hours} hours):\n"
                   f"{link(settings, 'verify-email', raw)}\n\nIf you did not ask for this, ignore this email.")


async def send_notice(db: Session, settings: Settings, customer_id: str, subject: str, text: str, *, essential: bool, mailer=None) -> bool:
    """A billing or system notice by email. Not sent when email is off, when the customer is unknown, or (for non-essential notices) when the customer opted out. Never raises."""
    try:
        mailer = mailer or build_mailer(settings)
        if mailer is None:
            return False
        customer = db.get(Customer, customer_id)
        if customer is None or not customer.is_active:
            return False
        state = db.get(CustomerEmailState, customer_id)
        if not essential and state is not None and state.notices_opt_out:
            return False
        return await mailer.send(Message(customer.email, subject, text))
    except Exception as exc:  # noqa: BLE001
        logger.warning("a notice email could not be prepared (%s)", type(exc).__name__)
        return False
