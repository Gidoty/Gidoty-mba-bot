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

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import Settings
from ...models import Customer, PaymentEvent, PaymentReference
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
    customer: Customer = Depends(get_current_customer),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
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
    customer.paystack_reference = data["reference"]  # unchanged: the latest reference, as before
    if settings.paystack_verify_mode != "off":
        _record_issued_reference(db, customer.id, data["reference"], settings)
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
    mode = settings.paystack_verify_mode
    if mode == "enforce":
        return await _apply_verified_event(db, event, settings)
    if mode == "log":
        return await _apply_with_shadow_verification(db, event, settings)
    apply_payment_event(db, event)
    return {"ok": True}


# --------------------------------------------------------------------- F-01: strict verification
#
# ENFORCE mode (PAYSTACK_STRICT_VERIFY=1 or enforce). The signature check proves the request came from Paystack; it does
# not prove WHICH transaction it describes. A charge for any amount, made by anyone, carrying someone's customer id in its
# metadata would otherwise unlock lifetime access. So each event is re-checked against Paystack's own record of the
# transaction and against what this application issued.


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


_FEE_ROUNDING = 1  # minor units of slack in the fee ARITHMETIC only (amount - requested_amount vs fees); the price is compared exactly


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def amount_problem(data: dict, expected: int) -> str | None:
    """Is what Paystack says was paid exactly the price, with anything extra explained by Paystack's own fee figure?

    Paystack reports `amount` (what the customer was charged), usually `requested_amount` (the price that was asked
    for) and `fees` (Paystack's charge). Two legitimate shapes:
      * the business pays the charges: amount == price;
      * the customer pays the charges: amount == price + fees (so amount is larger than the price).
    The price itself is never inferred from the amount alone. A larger amount is accepted only when `fees` accounts
    for the whole difference, so overpaying, or a fee figure that does not add up, is refused rather than guessed at.
    Returns None if acceptable, else a short reason code."""
    amount, requested, fees = data.get("amount"), data.get("requested_amount"), data.get("fees")
    if not _is_int(amount) or (requested is not None and not _is_int(requested)) or (fees is not None and not _is_int(fees)):
        return "amount_unreadable"
    if amount < expected:
        return "underpaid"
    if requested is not None:
        if requested < expected:
            return "underpaid"
        if requested != expected:
            return "amount_mismatch"
        if amount == requested:
            return None  # business pays the charges
    elif amount == expected:
        return None  # business pays the charges; this response carries no requested_amount
    # Something is on top of the price: it must be exactly Paystack's fee.
    if fees is None or fees < 0 or fees > amount:
        return "amount_unexplained"
    base = requested if requested is not None else amount - fees
    if requested is not None:
        return None if abs(amount - requested - fees) <= _FEE_ROUNDING else "fee_relation_unexpected"
    return None if base == expected else ("underpaid" if base < expected else "amount_mismatch")  # exact: no slack on the price


def check_verified_transaction(body: dict, *, reference: str, customer_id: str, settings: Settings,
                               expected_amount: int | None = None, expected_currency: str | None = None) -> str | None:
    """None if Paystack's record matches what we issued, else a short reason code (never exchange text).
    `expected_*` default to today's configured price; a recorded issue-time price is passed when there is one."""
    amount = settings.paystack_amount if expected_amount is None else expected_amount
    currency = settings.paystack_currency if expected_currency is None else expected_currency
    if not amount or amount <= 0:
        return "expected_amount_not_configured"
    if not body.get("status"):
        return "provider_rejected_lookup"
    data = body.get("data") or {}
    if data.get("status") != "success":
        return "transaction_not_successful"
    if data.get("reference") != reference:
        return "reference_differs"
    if str(data.get("currency", "")).upper() != str(currency).upper():
        return "currency_mismatch"
    problem = amount_problem(data, amount)
    if problem is not None:
        return problem
    if (data.get("metadata") or {}).get("customer_id") != customer_id:
        return "customer_mismatch"
    return None


def _mask(value: str | None) -> str:
    """A customer id is a random UUID, not personal data, but logs carry only its first 8 characters."""
    return "-" if not value else f"{str(value)[:8]}..."


def _tail(value: str | None) -> str:
    return "-" if not value else f"...{str(value)[-6:]}"


def _record_issued_reference(db: Session, customer_id: str, reference: str, settings: Settings) -> None:
    """Remember a reference issued at /initialize. Never allowed to break checkout: a failure is logged and ignored."""
    try:
        with db.begin_nested():
            db.add(PaymentReference(provider="paystack", reference=reference, customer_id=customer_id,
                                    amount=settings.paystack_amount, currency=str(settings.paystack_currency).upper()))
            db.flush()
    except Exception:  # noqa: BLE001 - bookkeeping only; the payment link was already created
        logger.warning("could not record an issued payment reference; checkout continues")


@dataclass
class _Pre:
    """What the database alone says about an event, read BEFORE anything is applied."""
    reason: str | None = None
    duplicate: bool = False
    reference: str | None = None
    customer_id: str | None = None
    event_key: str = ""
    expected_amount: int | None = None
    expected_currency: str | None = None


def _precheck(db: Session, event: dict, settings: Settings) -> _Pre | None:
    """None if this is not a charge event. Otherwise the database-only part of the verdict."""
    if event.get("event") != "charge.success":
        return None
    data = event.get("data") or {}
    reference = data.get("reference")
    customer_id = (data.get("metadata") or {}).get("customer_id")
    pre = _Pre(reference=reference, customer_id=customer_id)
    if not reference or not customer_id:
        pre.reason = "missing_reference_or_customer"
        return pre
    pre.event_key = f"charge.success:{reference}"[:160]
    if db.query(PaymentEvent).filter_by(provider="paystack", event_key=pre.event_key).first() is not None:
        pre.duplicate = True
        return pre
    customer = db.get(Customer, customer_id)
    if customer is None:
        pre.reason = "unknown_customer"
        return pre
    issued = db.query(PaymentReference).filter_by(provider="paystack", reference=reference).first()
    if issued is not None:
        if issued.customer_id != customer_id:
            pre.reason = "reference_issued_to_other"
            return pre
        pre.expected_amount, pre.expected_currency = issued.amount, issued.currency  # the price at the time of issue
    elif customer.paystack_reference != reference:
        # Neither recorded as issued nor the latest reference stored for this customer (references issued before this
        # table existed are still honoured through the stored latest one, exactly as before).
        pre.reason = "reference_not_issued"
    return pre


async def _final_reason(pre: _Pre, settings: Settings) -> str | None:
    """The network half of the verdict. Raises PaystackUnavailable if Paystack cannot be asked."""
    body = await _verify_with_paystack(pre.reference, settings)
    return check_verified_transaction(body, reference=pre.reference, customer_id=pre.customer_id, settings=settings,
                                      expected_amount=pre.expected_amount, expected_currency=pre.expected_currency)


async def _apply_verified_event(db: Session, event: dict, settings: Settings) -> dict:
    """ENFORCE mode. Idempotent. Returns 200 for anything final (applied, duplicate, rejected) so Paystack stops
    retrying, and 503 only when Paystack could not be asked, so it retries later. A rejected event grants nothing."""
    pre = _precheck(db, event, settings)
    if pre is None:
        return {"ok": True}
    if pre.duplicate:
        return {"ok": True, "duplicate": True}
    if pre.reason is None:
        try:
            pre.reason = await _final_reason(pre, settings)
        except PaystackUnavailable as exc:
            logger.warning("paystack verify unavailable (%s); asking Paystack to retry", exc)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "could not verify the transaction yet") from None
    if pre.reason is not None:
        logger.warning("paystack webhook rejected: reason=%s customer=%s ref=%s", pre.reason, _mask(pre.customer_id), _tail(pre.reference))
        return {"ok": True}

    customer = db.get(Customer, pre.customer_id)
    amount = pre.expected_amount if pre.expected_amount is not None else settings.paystack_amount
    currency = (pre.expected_currency or settings.paystack_currency).upper()
    outcome = "already_paid" if customer.has_paid else "applied"
    try:
        with db.begin_nested():
            db.add(PaymentEvent(provider="paystack", event_key=pre.event_key, reference=pre.reference, customer_id=pre.customer_id,
                                amount=amount, currency=currency, outcome=outcome))
            db.flush()
    except IntegrityError:
        return {"ok": True, "duplicate": True}  # a concurrent delivery of the same event won
    if outcome == "applied":
        customer.has_paid = True
        if customer.account is not None:
            customer.account.is_active = True
    return {"ok": True}


# --------------------------------------------------------------------- LOG mode
#
# Runs the whole verification and writes the verdict to the log, but applies the event exactly as the code did before any
# of this existed. It can only ADD a log line: every failure inside it is swallowed, it writes nothing to the database,
# and it runs after today's handling so it cannot change the outcome. The log carries a masked customer id, the last
# six characters of the reference and a reason code: no email, no secret, no raw Paystack payload.

_SHADOW_TIMEOUT_SECONDS = 6


def _legacy_would_grant(db: Session, event: dict) -> bool:
    if event.get("event") != "charge.success":
        return False
    data = event.get("data") or {}
    customer_id = (data.get("metadata") or {}).get("customer_id")
    return data.get("status") == "success" and bool(customer_id) and db.get(Customer, customer_id) is not None


async def _apply_with_shadow_verification(db: Session, event: dict, settings: Settings) -> dict:
    pre = legacy = None
    try:
        pre = _precheck(db, event, settings)  # read first: today's handling overwrites the stored reference
        legacy = _legacy_would_grant(db, event)
    except Exception:  # noqa: BLE001
        logger.warning("paystack verify[log]: could not read the database for the shadow check")
    apply_payment_event(db, event)  # today's behaviour, unchanged
    if pre is not None:
        try:
            await asyncio.wait_for(_log_shadow_verdict(pre, legacy, settings), timeout=_SHADOW_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - log mode must never affect a payment
            logger.warning("paystack verify[log]: shadow check failed (verdict not logged)")
    return {"ok": True}


async def _log_shadow_verdict(pre: _Pre, legacy: bool | None, settings: Settings) -> None:
    reason, state = pre.reason, "reject"
    if pre.duplicate:
        state, reason = "duplicate", "already_processed"
    elif reason is None:
        try:
            reason = await _final_reason(pre, settings)
        except PaystackUnavailable as exc:
            state, reason = "unavailable", str(exc)[:40]
    if state == "reject" and reason is None:
        state, reason = "accept", "ok"
    level = logging.INFO if state in ("accept", "duplicate") else logging.WARNING
    logger.log(level, "paystack verify[log]: verdict=%s reason=%s today=%s customer=%s ref=%s", state, reason,
               "grants" if legacy else "ignores", _mask(pre.customer_id), _tail(pre.reference))
