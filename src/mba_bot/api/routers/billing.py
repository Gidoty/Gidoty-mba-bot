"""Paystack one-time payment + webhook.

Lifetime access, not a subscription: a customer pays once
(`/initialize`), Paystack's webhook confirms it, and `Customer.has_paid`
never needs to flip back - there's no renewal or cancellation to track,
unlike a subscription model.

`apply_payment_event` is kept separate from the webhook endpoint itself
so it can be unit tested with a hand-built event dict, without a real
Paystack signature or network call - see tests/test_api_billing.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ...config import Settings
from ...models import Customer
from ..deps import get_current_customer, get_db, get_settings
from ..schemas import PaymentInitOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/billing", tags=["billing"])

_PAYSTACK_API = "https://api.paystack.co"


def _require_paystack_configured(settings: Settings) -> None:
    if not settings.paystack_secret_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PAYSTACK_SECRET_KEY is not configured")


@router.post("/initialize", response_model=PaymentInitOut)
def initialize_payment(
    customer: Customer = Depends(get_current_customer), settings: Settings = Depends(get_settings)
) -> PaymentInitOut:
    _require_paystack_configured(settings)
    if customer.has_paid:
        raise HTTPException(status.HTTP_409_CONFLICT, "this customer already has lifetime access")

    response = httpx.post(
        f"{_PAYSTACK_API}/transaction/initialize",
        headers={"Authorization": f"Bearer {settings.paystack_secret_key}"},
        json={
            "email": customer.email,
            "amount": settings.paystack_amount,
            "currency": settings.paystack_currency,
            "callback_url": settings.frontend_origin,
            "metadata": {"customer_id": customer.id},
        },
        timeout=15,
    )
    body = response.json()
    if response.is_error or not body.get("status"):
        logger.error("paystack initialize failed: %s", body.get("message"))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not start payment with Paystack")

    data = body["data"]
    customer.paystack_reference = data["reference"]
    return PaymentInitOut(authorization_url=data["authorization_url"])


def apply_payment_event(db: Session, event: dict) -> None:
    """Mark a customer as paid from a verified Paystack event. Looks the
    customer up by the `customer_id` passed as metadata at `/initialize`
    time, which Paystack echoes back unchanged on the webhook."""
    if event.get("event") != "charge.success":
        return

    data = event.get("data") or {}
    if data.get("status") != "success":
        return

    customer_id = (data.get("metadata") or {}).get("customer_id")
    if not customer_id:
        return

    customer = db.get(Customer, customer_id)
    if customer is None:
        logger.warning("paystack webhook: no customer matches id=%s", customer_id)
        return

    customer.has_paid = True
    customer.paystack_reference = data.get("reference")
    if customer.account is not None:
        customer.account.is_active = True


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def billing_webhook(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    _require_paystack_configured(settings)

    payload = await request.body()
    signature = request.headers.get("x-paystack-signature", "")
    expected = hmac.new(settings.paystack_secret_key.encode("utf-8"), payload, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid webhook signature")

    event = json.loads(payload)
    apply_payment_event(db, event)
    return {"ok": True}
