"""What the customer's USDT panel shows (docs/safety/crypto_payment_panel.md). Reads our own database only: no NOWPayments call, no write, no secret.

The stages are plain: waiting for the transfer, transfer seen and confirming, confirmed, underpaid, expired, failed, refunded. They come from the payment events the one apply rule (nowpayments.process_record) wrote for the
invoice, newest last. Nothing here grants access or decides anything: an invoice is `confirmed` here only because the apply rule marked it paid."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import BillingPayment, PaymentEvent
from . import nowpayments as npay

WAITING, CONFIRMING, CONFIRMED, UNDERPAID, EXPIRED, FAILED, REFUNDED = "waiting", "confirming", "confirmed", "underpaid", "expired", "failed", "refunded"
NO_PAYMENT_SEEN, UNCLEAR = "no_payment_seen", "unclear"

_BY_OUTCOME = {"seen:waiting": WAITING, "seen:confirming": CONFIRMING, "seen:confirmed": CONFIRMING, "seen:sending": CONFIRMING, "underpaid": UNDERPAID, "expired": EXPIRED, "failed": FAILED,
               "refunded": REFUNDED, "applied": CONFIRMED}

TEXT = {
    WAITING: "Waiting for your transfer.",
    CONFIRMING: "We can see your transfer and are waiting for the network to confirm it.",
    CONFIRMED: "Payment confirmed.",
    UNDERPAID: "We received less than the price.",
    EXPIRED: "This payment window expired.",
    FAILED: "The payment failed.",
    REFUNDED: "The payment was refunded.",
    NO_PAYMENT_SEEN: "The payment provider has not told us about a transfer for this invoice yet. If you have just sent it, wait a minute and try again.",
    UNCLEAR: "The payment provider answered, but the answer does not match this invoice. Please send the reference to support.",
}


def _aware(v: datetime | None) -> datetime | None:
    return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc))


def money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.quantize(Decimal('0.01')):.2f}"


def stage_of(bp: BillingPayment, events: list[PaymentEvent]) -> str:
    if bp.status == "paid":
        return CONFIRMED
    if not events:
        return WAITING
    return _BY_OUTCOME.get(events[-1].outcome, WAITING)


def short_usd(bp: BillingPayment, events: list[PaymentEvent]) -> str | None:
    """How much the last underpaid payment was short, in USD, or None when it is unknown."""
    for e in reversed(events):
        if e.outcome == "underpaid":
            if e.amount is None or e.amount >= bp.amount_minor:
                return None
            return money(Decimal(bp.amount_minor - e.amount) / Decimal(100))
    return None


def short_from_value(bp: BillingPayment, usd_value: Decimal | None) -> str | None:
    if usd_value is None:
        return None
    price = npay.expected_usd(bp)
    return money(price - usd_value) if usd_value < price else None


def invoice_view(db: Session, customer_id: str, settings: Settings, now: datetime) -> dict:
    """The customer's latest crypto invoice as the panel needs it, or {"invoice": None}. An invoice row that never got a payment link (the provider refused to create it) is skipped."""
    mode = settings.nowpayments_ipn_mode
    out: dict = {"mode": mode, "check_available": mode in ("observe", "enforce"), "server_time": now.isoformat(), "window_minutes": npay.PAYMENT_WINDOW_MINUTES, "invoice": None}
    rows = db.execute(select(BillingPayment).where(BillingPayment.provider == npay.PROVIDER, BillingPayment.customer_id == customer_id).order_by(BillingPayment.issued_at.desc()).limit(5)).scalars().all()
    for bp in rows:
        events = db.execute(select(PaymentEvent).where(PaymentEvent.provider == npay.PROVIDER, PaymentEvent.reference == bp.reference).order_by(PaymentEvent.created_at, PaymentEvent.id)).scalars().all()
        if bp.status == "failed" and not events:
            continue
        issued = _aware(bp.issued_at)
        ends = issued + timedelta(minutes=npay.PAYMENT_WINDOW_MINUTES)
        stage = stage_of(bp, events)
        detected = next((_aware(e.created_at) for e in events if e.outcome in npay.TRANSFER_OUTCOMES), None)
        out["invoice"] = {
            "reference": bp.reference, "price_usd": money(npay.expected_usd(bp)), "issued_at": issued.isoformat(), "window_ends_at": ends.isoformat(), "stage": stage,
            "short_usd": short_usd(bp, events) if stage == UNDERPAID else None, "detected_at": detected.isoformat() if detected else None,
            "detected_after_expiry": bool(detected is not None and detected > ends),
        }
        break
    return out


def stage_from_verdict(kind: str, status: str | None) -> str:
    """The stage a payment record read back from the provider means, for the answer to the check button (observe mode has no database stage to read)."""
    if kind == "pending":
        return WAITING if status == "waiting" else CONFIRMING
    return {"apply": CONFIRMED, "underpaid": UNDERPAID, "expired": EXPIRED, "failed": FAILED, "refunded": REFUNDED}.get(kind, UNCLEAR)
