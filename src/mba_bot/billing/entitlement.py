"""Who may open NEW live trades: the one function that replaces every `Customer.has_paid` read.

Stage 0 of designs/03 (subscription billing). It contains no provider code, holds no key and talks to nobody.

    SUBSCRIPTION_BILLING_ENABLED off (the default):  is_entitled(...) is exactly `customer.has_paid`. It does not even
    touch the subscription relationship, so a deployment without the new tables behaves as before. The whole existing
    suite passing unchanged pins this.

    SUBSCRIPTION_BILLING_ENABLED on:
      no subscription row, has_paid           legacy_lifetime  entitled for ever (D1: existing paid customers keep access)
      no subscription row, not has_paid       none             not entitled
      active, now before period end           active           entitled
      active, period end up to the grace end  grace            entitled (D2: 5 days)
      active, after the grace end             expired          not entitled
      cancelled, now before period end        cancelled        entitled until the period end, no grace
      cancelled, at or after period end       expired          not entitled
      any other status, or a missing end      expired          not entitled (fail closed)

A subscription row governs when it exists: a lifetime customer who somehow also has a row is judged by the row.

"Entitled" only ever decides NEW live entries (onboarding.resolve_trading_mode). An open position is managed with its own
is_paper flag to its exit whatever this returns; nothing here closes, cancels or touches a position, a credential or an
account. Time comparisons are at the second: the paid period ends AT current_period_end (exclusive), grace ends AT the
grace end (exclusive).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

from ..config import Settings

ACTIVE_STATUS = "active"
CANCELLED_STATUS = "cancelled"


class EntitlementState(str, Enum):
    LEGACY_LIFETIME = "legacy_lifetime"
    ACTIVE = "active"
    GRACE = "grace"
    CANCELLED = "cancelled"  # cancelled, but still inside the period already paid for
    EXPIRED = "expired"
    NONE = "none"


ENTITLED_STATES = frozenset(
    {EntitlementState.LEGACY_LIFETIME, EntitlementState.ACTIVE, EntitlementState.GRACE, EntitlementState.CANCELLED}
)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands datetimes back without a zone. Everything is stored in UTC, so a naive value is UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def grace_end(subscription, grace_days: int) -> datetime | None:
    """When the grace window closes: the stored `grace_until` if it is later than the period end, else period end + grace days."""
    end = _aware(subscription.current_period_end)
    if end is None:
        return None
    stored = _aware(getattr(subscription, "grace_until", None))
    if stored is not None:
        return max(stored, end)
    return end + timedelta(days=max(0, int(grace_days)))


def subscription_state(subscription, now: datetime, grace_days: int) -> EntitlementState:
    now = _aware(now)
    end = _aware(subscription.current_period_end)
    if end is None:
        return EntitlementState.EXPIRED  # a subscription with no period end is malformed: fail closed
    status = subscription.status
    if status == CANCELLED_STATUS:
        return EntitlementState.CANCELLED if now < end else EntitlementState.EXPIRED
    if status != ACTIVE_STATUS:
        return EntitlementState.EXPIRED
    if now < end:
        return EntitlementState.ACTIVE
    if now < grace_end(subscription, grace_days):
        return EntitlementState.GRACE
    return EntitlementState.EXPIRED


def entitlement_state(customer, *, settings: Settings, now: datetime | None = None) -> EntitlementState:
    """The state the entitlement rule puts this customer in. Only meaningful with the flag on; with it off this reports
    the has_paid view (legacy_lifetime or none) and never reads the subscription."""
    if not settings.subscription_billing_enabled:
        return EntitlementState.LEGACY_LIFETIME if customer.has_paid else EntitlementState.NONE
    now = now or datetime.now(timezone.utc)
    subscription = customer.subscription
    if subscription is None:
        return EntitlementState.LEGACY_LIFETIME if customer.has_paid else EntitlementState.NONE
    return subscription_state(subscription, now, settings.subscription_grace_days)


def is_entitled(customer, *, settings: Settings, now: datetime | None = None) -> bool:
    if not settings.subscription_billing_enabled:
        return bool(customer.has_paid)  # exactly today's behaviour; the subscription is not read
    return entitlement_state(customer, settings=settings, now=now) in ENTITLED_STATES
