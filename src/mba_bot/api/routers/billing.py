"""Stripe Checkout + webhook + billing portal.

`apply_subscription_event` is kept separate from the webhook endpoint
itself so it can be unit tested with a hand-built event dict, without a
real Stripe signature or network call - see tests/test_api_billing.py.
"""
from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ...config import Settings
from ...models import Customer
from ..deps import get_current_customer, get_db, get_settings
from ..schemas import BillingPortalOut, CheckoutSessionOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/billing", tags=["billing"])

# Stripe subscription statuses that mean "this customer's trading account
# should be allowed to run"; anything else (past_due, canceled, unpaid,
# incomplete_expired, ...) pauses it.
_ACTIVE_STATUSES = {"active", "trialing"}


def _require_stripe_configured(settings: Settings) -> None:
    if not settings.stripe_secret_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "STRIPE_SECRET_KEY is not configured")
    stripe.api_key = settings.stripe_secret_key


@router.post("/checkout-session", response_model=CheckoutSessionOut)
def create_checkout_session(
    customer: Customer = Depends(get_current_customer), settings: Settings = Depends(get_settings)
) -> CheckoutSessionOut:
    _require_stripe_configured(settings)
    if not settings.stripe_price_id:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "STRIPE_PRICE_ID is not configured")

    if not customer.stripe_customer_id:
        stripe_customer = stripe.Customer.create(email=customer.email)
        customer.stripe_customer_id = stripe_customer["id"]

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer.stripe_customer_id,
        line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
        success_url=f"{settings.frontend_origin}/billing/success",
        cancel_url=f"{settings.frontend_origin}/billing/cancel",
    )
    return CheckoutSessionOut(checkout_url=session["url"])


@router.get("/portal", response_model=BillingPortalOut)
def billing_portal(
    customer: Customer = Depends(get_current_customer), settings: Settings = Depends(get_settings)
) -> BillingPortalOut:
    _require_stripe_configured(settings)
    if not customer.stripe_customer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no billing account yet - subscribe first")

    portal_session = stripe.billing_portal.Session.create(
        customer=customer.stripe_customer_id, return_url=settings.frontend_origin
    )
    return BillingPortalOut(portal_url=portal_session["url"])


def apply_subscription_event(db: Session, event: dict) -> None:
    """Update `Customer.subscription_status` and the linked account's
    `is_active` from a verified Stripe event. Looks the customer up by
    `stripe_customer_id`, which every event type handled here carries."""
    event_type = event.get("type", "")
    data = (event.get("data") or {}).get("object") or {}

    if event_type == "checkout.session.completed":
        stripe_customer_id = data.get("customer")
        status_value = "active"
    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        stripe_customer_id = data.get("customer")
        status_value = data.get("status", "canceled")
    else:
        return

    if not stripe_customer_id:
        return

    customer = db.query(Customer).filter(Customer.stripe_customer_id == stripe_customer_id).first()
    if customer is None:
        logger.warning("stripe webhook: no customer matches stripe_customer_id=%s", stripe_customer_id)
        return

    customer.subscription_status = status_value
    if customer.account is not None:
        customer.account.is_active = status_value in _ACTIVE_STATUSES


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def billing_webhook(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    if not settings.stripe_webhook_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "STRIPE_WEBHOOK_SECRET is not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid webhook signature") from exc

    apply_subscription_event(db, event)
    return {"ok": True}
