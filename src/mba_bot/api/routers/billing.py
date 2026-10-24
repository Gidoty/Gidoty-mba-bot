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
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import Settings
from ...models import Customer, PaymentEvent
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
    if settings.paystack_strict_verify:
        return await _apply_verified_event(db, event, settings)
    apply_payment_event(db, event)
    return {"ok": True}


# --------------------------------------------------------------------- F-01: strict verification
#
# Only used when PAYSTACK_STRICT_VERIFY is on. The signature check above proves the request came from
# Paystack; it does not prove WHICH transaction it describes. A charge for any amount, made by anyone,
# carrying someone's customer id in its metadata would otherwise unlock lifetime access. So each event is
# re-checked against Paystack's own record of the transaction and against what this application issued.


class PaystackUnavailable(RuntimeError):
    """Paystack could not be asked (network error, 5xx, unreadable answer). Nothing was decided or recorded."""


async def _verify_with_paystack(reference: str, settings: Settings) -> dict:
    """Paystack's own record of the transaction. Raises PaystackUnavailable if it cannot be read."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{_PAYSTACK_API}/transaction/verify/{quote(reference, safe='')}",
                headers={"Authorization": f"Bearer {settings.paystack_secret_key}"},
            )
    except httpx.HTTPError as exc:
        raise PaystackUnavailable(type(exc).__name__) from None
    if response.status_code == 404:
        return {"status": False}
    if response.status_code >= 500 or response.status_code == 429:
        raise PaystackUnavailable(f"http {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise PaystackUnavailable("unreadable response") from None


def check_verified_transaction(body: dict, *, reference: str, customer_id: str, settings: Settings) -> str | None:
    """None if Paystack's record matches what we issued, else a short reason code (never exchange text)."""
    if not settings.paystack_amount or settings.paystack_amount <= 0:
        return "expected_amount_not_configured"
    if not body.get("status"):
        return "provider_rejected_lookup"
    data = body.get("data") or {}
    if data.get("status") != "success":
        return "transaction_not_successful"
    if data.get("reference") != reference:
        return "reference_differs"
    if str(data.get("currency", "")).upper() != str(settings.paystack_currency).upper():
        return "currency_mismatch"
    paid = data.get("requested_amount", data.get("amount"))
    if paid != settings.paystack_amount or (data.get("amount") or 0) < settings.paystack_amount:
        return "amount_mismatch"
    if (data.get("metadata") or {}).get("customer_id") != customer_id:
        return "customer_mismatch"
    return None


async def _apply_verified_event(db: Session, event: dict, settings: Settings) -> dict:
    """Idempotent. Returns 200 for anything final (applied, duplicate, rejected) so Paystack stops retrying, and
    503 only when Paystack could not be asked, so it retries later. A rejected event grants nothing."""
    if event.get("event") != "charge.success":
        return {"ok": True}
    data = event.get("data") or {}
    reference = data.get("reference")
    customer_id = (data.get("metadata") or {}).get("customer_id")
    if not reference or not customer_id:
        logger.warning("paystack webhook rejected: reason=missing_reference_or_customer")
        return {"ok": True}
    event_key = f"charge.success:{reference}"[:160]

    if db.query(PaymentEvent).filter_by(provider="paystack", event_key=event_key).first() is not None:
        return {"ok": True, "duplicate": True}
    customer = db.get(Customer, customer_id)
    if customer is None:
        logger.warning("paystack webhook rejected: reason=unknown_customer reference=%s", reference)
        return {"ok": True}
    if customer.paystack_reference != reference:
        # Not the reference this application issued for this customer at /initialize.
        logger.warning("paystack webhook rejected: reason=reference_not_issued reference=%s customer=%s", reference, customer_id)
        return {"ok": True}
    try:
        body = await _verify_with_paystack(reference, settings)
    except PaystackUnavailable as exc:
        logger.warning("paystack verify unavailable (%s); asking Paystack to retry", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "could not verify the transaction yet") from None
    reason = check_verified_transaction(body, reference=reference, customer_id=customer_id, settings=settings)
    if reason is not None:
        logger.warning("paystack webhook rejected: reason=%s reference=%s customer=%s", reason, reference, customer_id)
        return {"ok": True}

    outcome = "already_paid" if customer.has_paid else "applied"
    try:
        with db.begin_nested():
            db.add(PaymentEvent(provider="paystack", event_key=event_key, reference=reference, customer_id=customer_id,
                                amount=settings.paystack_amount, currency=settings.paystack_currency.upper(), outcome=outcome))
            db.flush()
    except IntegrityError:
        return {"ok": True, "duplicate": True}  # a concurrent delivery of the same event won
    if outcome == "applied":
        customer.has_paid = True
        if customer.account is not None:
            customer.account.is_active = True
    return {"ok": True}
