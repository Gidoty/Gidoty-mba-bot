"""Paystack subscriptions, Stage 1 of designs/03. Runs ONLY while SUBSCRIPTION_BILLING_ENABLED is on.

Rules this module keeps (owner and reviewer decisions, 10 Nov):
  * A webhook body is a TRIGGER. It names a reference, a subscription or an email; every fact that changes anything is
    re-read from Paystack (GET /transaction/verify/:reference, GET /subscription/:code) before it is used.
  * Every payment passes the ladder: signature (done by the router on the raw bytes), verify by API, reference issued by
    us or a renewal for a customer who has our subscription, currency NGN, amount (the existing amount_problem, strict: the
    price or price + Paystack's own fee, nothing else), customer match, replay protection (PaymentEvent + the unique
    billing_payments reference).
  * `current_period_end` is Paystack's own `next_payment_date` from the re-read subscription. This module does no
    month arithmetic. Anything outside a sanity bound, missing or not later than the current end on a renewal is not
    applied: Paystack is asked again later (HTTP 503) and the owner is alerted if it persists.
  * A failed renewal is a missed period: nothing is changed (the period end passing starts the grace window by itself, see
    entitlement.py) and the owner is alerted. A refund is recorded and alerted, never auto-revoked.
  * The subscription email token is stored encrypted and is never logged or returned.
  * Every function tolerates missing fields. A field that cannot be read means "do not decide".
Nothing here places, changes or cancels a trade, touches a credential, or sends money.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crypto_utils
from ..config import Settings
from ..models import BillingPayment, Customer, PaymentEvent, Subscription, SubscriptionToken
from ..notifications.telegram import TelegramNotifier
from ..protection_guard import should_alert
from . import paystack_sub as ps
from .paystack_sub import ProviderUnavailable  # noqa: F401  (re-exported for the router)

logger = logging.getLogger(__name__)

PROVIDER = "paystack"
SUBSCRIPTION_EVENTS = frozenset({"subscription.create", "subscription.not_renew", "subscription.disable", "invoice.payment_failed"})
ALERT_REPEAT_HOURS = 6.0
PENDING_APPLY_ALERT_AFTER = timedelta(minutes=10)

ALERTS = {
    "amount_underpaid": "Billing: a verified payment was LESS than the price. No access was granted.",
    "amount_problem": "Billing: a verified payment amount could not be explained by the price and Paystack's fee. No access was granted.",
    "payment_rejected": "Billing: a payment failed a safety check (customer, currency, plan or reference). No access was granted.",
    "renewal_failed": "Billing: a subscription renewal payment failed. The paid period is not extended; the grace window starts when the period ends.",
    "refund_seen": "Billing: Paystack reported a refund. Nothing was revoked; please review it.",
    "refund_unmatched": "Billing: Paystack reported a refund we could not match to a payment. Nothing was changed.",
    "card_not_reusable": "Billing: a customer paid with a card Paystack cannot reuse, so it will not renew by itself.",
    "plan_mismatch": "Billing: a subscription is on a different plan from the configured one. Nothing was changed.",
    "subscription_customer_mismatch": "Billing: a subscription belongs to a different email from our customer. Nothing was changed.",
    "unknown_subscription_status": "Billing: Paystack reported a subscription status we do not know. Nothing was changed.",
    "period_end_out_of_range": "Billing: Paystack's next payment date was missing or outside the expected range. The period was not changed.",
    "payment_pending_apply": "Billing: a verified payment has not been applied to a subscription yet (waiting for Paystack's subscription record).",
    "subscription_cancelled": "Billing: a customer's subscription will not renew. Access continues until the paid period ends.",
    "unknown_customer": "Billing: a subscription payment event matched no customer. Nothing was changed.",
}


class RetryLater(RuntimeError):
    """The facts are not settled on Paystack's side yet. The router answers 503 so Paystack delivers the event again."""


def _mask(value: str | None) -> str:
    return "-" if not value else f"{str(value)[:8]}..."


def _tail(value: str | None) -> str:
    return "-" if not value else f"...{str(value)[-6:]}"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def alert_owner(settings: Settings, code: str, customer_id: str | None, now: datetime) -> None:
    """A fixed message (no email, no amount, no provider text). Logged always; sent to Telegram at most once per code and
    customer per 6 hours, only if a bot token and BILLING_ALERT_CHAT_ID are configured. Never raises."""
    logger.warning("billing alert: code=%s customer=%s", code, _mask(customer_id))
    try:
        if not (settings.telegram_bot_token and settings.billing_alert_chat_id):
            return
        if not should_alert(customer_id or "-", "billing", code, now=now, repeat_hours=ALERT_REPEAT_HOURS):
            return
        text = f"{ALERTS.get(code, 'Billing: an event needs your attention.')} Customer {_mask(customer_id)}"
        await TelegramNotifier(settings.telegram_bot_token).send(settings.billing_alert_chat_id, text)
    except Exception:  # noqa: BLE001 - an alert must never break a webhook
        logger.warning("billing alert could not be delivered")


# ----------------------------------------------------------------- checkout (new customers)

def new_reference() -> str:
    return f"mbasub_{uuid.uuid4().hex[:24]}"


def record_issued_payment(db: Session, customer_id: str, reference: str, settings: Settings) -> BillingPayment:
    """Remember a checkout we issued: the price we asked for, so the payment can be matched to it later."""
    row = BillingPayment(customer_id=customer_id, provider=PROVIDER, reference=reference, amount_minor=settings.paystack_plan_amount,
                         currency=ps.PLAN_CURRENCY, status="pending")
    db.add(row)
    db.flush()
    return row


# ----------------------------------------------------------------- finding the customer

def _customer_by_email(db: Session, email: str | None) -> Customer | None:
    if not email:
        return None
    return db.query(Customer).filter(func.lower(Customer.email) == email.strip().lower()).first()


def customer_for_event(db: Session, data: dict) -> Customer | None:
    """Which customer an event is about. Only a candidate: the facts are re-read from Paystack before anything changes."""
    code = ps.event_subscription_code(data)
    if code:
        row = db.query(Subscription).filter_by(provider=PROVIDER, provider_subscription_ref=code).first()
        if row is not None:
            return db.get(Customer, row.customer_id)
    ref = ps.event_reference(data)
    if ref:
        bp = db.query(BillingPayment).filter_by(provider=PROVIDER, reference=ref).first()
        if bp is not None:
            return db.get(Customer, bp.customer_id)
    return _customer_by_email(db, ps.event_customer_email(data))


def is_subscription_charge(db: Session, data: dict, settings: Settings) -> bool:
    """Is this charge.success one of ours (so the subscription path handles it) or a legacy activation payment (which the
    existing handler keeps handling, unchanged)? Any doubt answers False: the legacy path then rejects or ignores it."""
    ref = ps.event_reference(data)
    if ref and db.query(BillingPayment).filter_by(provider=PROVIDER, reference=ref).first() is not None:
        return True
    plan = ps.event_plan_code(data)
    if plan and settings.paystack_plan_code and plan == settings.paystack_plan_code:
        return True
    customer = _customer_by_email(db, ps.event_customer_email(data))
    return bool(customer is not None and customer.subscription is not None and customer.subscription.provider == PROVIDER)


# ----------------------------------------------------------------- payments

def _expected_base(db: Session, customer_id: str, settings: Settings) -> int:
    """The price this customer's payments are checked against: the last verified payment's price (so a customer whose plan
    price was later raised, for new customers only, keeps being checked against what they signed up for), else the plan price."""
    last = (db.query(BillingPayment).filter_by(provider=PROVIDER, customer_id=customer_id, status="paid")
            .order_by(BillingPayment.paid_at.desc()).first())
    return last.amount_minor if last is not None else settings.paystack_plan_amount


def _amount_problem(tx: dict, expected: int) -> str | None:
    from ..api.routers.billing import amount_problem  # the existing strict check, not copied; imported late to avoid a cycle
    return amount_problem(tx, expected)


def _judge_payment(tx: dict, bp: BillingPayment, customer: Customer, settings: Settings) -> tuple[str, str | None]:
    """('ok'|'underpaid'|'failed', reason). Pure: reads only what Paystack returned and what we issued."""
    if tx.get("reference") != bp.reference:
        return "failed", "reference_differs"
    if str(tx.get("currency", "")).upper() != ps.PLAN_CURRENCY or bp.currency.upper() != ps.PLAN_CURRENCY:
        return "failed", "currency_mismatch"
    problem = _amount_problem(tx, bp.amount_minor)
    if problem == "underpaid":
        return "underpaid", problem
    if problem is not None:
        return "failed", problem
    meta_id = ps.dig(tx, "metadata", "customer_id")
    email = ps.dig(tx, "customer", "email")
    if meta_id is not None and meta_id != customer.id:
        return "failed", "customer_mismatch"
    if email is not None and not ps.same_email(email, customer.email):
        return "failed", "customer_mismatch"
    if meta_id != customer.id and not ps.same_email(email, customer.email):
        return "failed", "customer_unconfirmed"
    plan = ps.event_plan_code(tx)
    if plan and settings.paystack_plan_code and plan != settings.paystack_plan_code:
        return "failed", "plan_mismatch"
    return "ok", None


def as_amount(tx: dict) -> int | None:
    return ps.as_int(tx.get("amount"))


# ----------------------------------------------------------------- the subscription

async def _find_subscription(customer: Customer, row: Subscription | None, hint_code: str | None, need_new: bool,
                             settings: Settings) -> ps.SubInfo | None:
    """The customer's Paystack subscription, as Paystack reports it now. With a new payment waiting to be applied, only an
    active subscription will do (an old cancelled one cannot carry the new payment), so a re-subscription is found by listing."""
    code = hint_code or (row.provider_subscription_ref if row is not None else None)
    if code:
        data = await ps.fetch_subscription(code, settings)
        info = ps.read_subscription(data) if data else None
        if info is not None and (not need_new or info.status in ("active", "attention")):
            return info
    if not (settings.paystack_plan_code and need_new):
        return None
    plan_id = await ps.fetch_plan_numeric_id(settings.paystack_plan_code, settings)
    cust = await ps.fetch_customer(customer.email, settings)
    cust_id = ps.as_int(cust.get("id")) if cust else None
    if plan_id is None or cust_id is None:
        return None
    candidates = [i for i in await ps.list_customer_subscriptions(plan_id, cust_id, settings)
                  if ps.as_str(i.get("status")) in ps.LIVE_STATUSES and ps.as_str(i.get("subscription_code"))]
    candidates.sort(key=lambda i: (ps.as_str(i.get("status")) in ("active", "attention"),
                                   ps.parse_dt(i.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    if not candidates:
        return None
    data = await ps.fetch_subscription(candidates[0]["subscription_code"], settings)
    return ps.read_subscription(data) if data else None


def _store_token(db: Session, row: Subscription, token: str | None, settings: Settings) -> None:
    if not token or not settings.encryption_key:
        return
    enc = crypto_utils.encrypt(token, key=settings.encryption_key)
    existing = db.query(SubscriptionToken).filter_by(subscription_id=row.id).first()
    if existing is None:
        db.add(SubscriptionToken(subscription_id=row.id, token_enc=enc))
    else:
        existing.token_enc = enc


async def reconcile(db: Session, customer: Customer, settings: Settings, now: datetime, alerts: list, *,
                    hint_code: str | None = None, new_payment: BillingPayment | None = None) -> dict:
    """Settle this customer's pending payments from Paystack's records, then bring our subscription row in line with
    Paystack's subscription. Idempotent: running it twice changes nothing the second time.

    Two phases, because SQLite's write lock must never be held across a network wait (see db.session_scope): PHASE A only
    reads (database reads and Paystack reads, nothing is written or flushed); PHASE B writes, with no await inside.
    `alerts` collects (code, customer_id) pairs; the caller sends them after the commit. Raises RetryLater, before any
    write, when Paystack's record has not caught up."""
    with db.no_autoflush:
        pending = (db.query(BillingPayment).filter_by(provider=PROVIDER, customer_id=customer.id, status="pending")
                   .order_by(BillingPayment.issued_at.desc()).limit(5).all())
        if new_payment is not None:
            pending.append(new_payment)
        verdicts = []  # (payment, tx, verdict, reason)
        for bp in pending:
            tx = await ps.fetch_transaction(bp.reference, settings)
            if tx is None or tx.get("status") != "success":
                continue  # not (yet) a successful payment on Paystack's side: it stays pending
            verdict, reason = _judge_payment(tx, bp, customer, settings)
            verdicts.append((bp, tx, verdict, reason))
        newly_paid = [(bp, tx) for bp, tx, v, _ in verdicts if v == "ok"]
        unapplied_times = [_aware(bp.paid_at) for bp in db.query(BillingPayment).filter_by(provider=PROVIDER, customer_id=customer.id, status="paid")
                           .filter(BillingPayment.period_end.is_(None)).all()]
        unapplied_times += [ps.parse_dt(tx.get("paid_at")) or now for _, tx in newly_paid]
        unapplied_times = sorted(t for t in unapplied_times if t is not None)
        row = customer.subscription
        info = await _find_subscription(customer, row, hint_code, need_new=bool(unapplied_times), settings=settings)

        # ---- decide, still without writing anything
        if info is None or info.code is None:
            if unapplied_times:
                if now - unapplied_times[0] > PENDING_APPLY_ALERT_AFTER:
                    alerts.append(("payment_pending_apply", customer.id))
                raise RetryLater("subscription not visible yet")
            return _settle_only(db, verdicts, now, alerts, customer)
        if settings.paystack_plan_code and info.plan_code and info.plan_code != settings.paystack_plan_code:
            alerts.append(("plan_mismatch", customer.id))
            return _settle_only(db, verdicts, now, alerts, customer)
        if not ps.same_email(info.email, customer.email):
            alerts.append(("subscription_customer_mismatch", customer.id))
            return _settle_only(db, verdicts, now, alerts, customer)
        status_ours = ps.our_status(info.status)
        if status_ours is None:
            alerts.append(("unknown_subscription_status", customer.id))
            return _settle_only(db, verdicts, now, alerts, customer)
        final_cancel = info.status in ("completed", "cancelled")
        new_end = None if final_cancel else info.next_payment_date
        if new_end is not None and new_end > now + timedelta(days=ps.MAX_PERIOD_DAYS):
            new_end = None
        if unapplied_times:
            old_end = _aware(row.current_period_end) if row is not None else None
            if new_end is None:
                alerts.append(("period_end_out_of_range", customer.id))
                raise RetryLater("no usable next payment date")
            if new_end <= now or (old_end is not None and new_end <= old_end):
                raise RetryLater("period not advanced yet")  # Paystack's record has not caught up with the payment

    # ---- PHASE B: writes only, no await
    if not _settle(db, customer, verdicts, now, alerts):
        return {"ok": True, "duplicate": True}
    if info.reusable is False and unapplied_times:
        alerts.append(("card_not_reusable", customer.id))
    if unapplied_times:
        start = unapplied_times[-1]
        if row is None:
            row = Subscription(customer_id=customer.id, status=status_ours, current_period_start=start, current_period_end=new_end,
                               provider=PROVIDER, provider_subscription_ref=info.code)
            db.add(row)
            if customer.account is not None:
                customer.account.is_active = True  # same as the existing activation flow does on first payment
        else:
            row.status, row.current_period_start, row.current_period_end = status_ours, start, new_end
            row.provider, row.provider_subscription_ref = PROVIDER, info.code
        db.flush()
        for bp in db.query(BillingPayment).filter_by(provider=PROVIDER, customer_id=customer.id, status="paid").filter(BillingPayment.period_end.is_(None)):
            bp.period_start, bp.period_end = start, new_end
    elif row is not None and row.provider == PROVIDER and row.provider_subscription_ref == info.code:
        # No new payment: only the status can change. The paid period itself is never edited without a payment.
        if row.status != status_ours:
            if status_ours == "cancelled" and row.status == "active":
                alerts.append(("subscription_cancelled", customer.id))
            row.status = status_ours
    else:
        return {"ok": True}
    _store_token(db, row, info.email_token, settings)
    return {"ok": True}


def _settle_only(db: Session, verdicts: list, now: datetime, alerts: list, customer: Customer) -> dict:
    _settle(db, customer, verdicts, now, alerts)
    return {"ok": True}


def _settle(db: Session, customer: Customer, verdicts: list, now: datetime, alerts: list) -> bool:
    """Write the outcome of each verified payment. False if a concurrent delivery of the same payment already won."""
    for bp, tx, verdict, reason in verdicts:
        if bp.id is None or db.get(BillingPayment, bp.id) is None:
            try:
                with db.begin_nested():
                    db.add(bp)
                    db.flush()
            except IntegrityError:
                return False
        if verdict != "ok":
            bp.status = verdict
            logger.warning("subscription payment rejected: reason=%s customer=%s ref=%s", reason, _mask(customer.id), _tail(bp.reference))
            alerts.append(("amount_underpaid" if verdict == "underpaid" else
                           ("amount_problem" if reason and reason.startswith(("amount", "fee")) else "payment_rejected"), customer.id))
            continue
        try:
            with db.begin_nested():
                db.add(PaymentEvent(provider=PROVIDER, event_key=f"sub-charge:{bp.reference}"[:160], reference=bp.reference,
                                    customer_id=customer.id, amount=as_amount(tx), currency=ps.PLAN_CURRENCY, outcome="applied"))
                db.flush()
        except IntegrityError:
            return False
        bp.status = "paid"
        bp.paid_at = ps.parse_dt(tx.get("paid_at")) or now
        paystack_id = ps.as_int(tx.get("id"))
        bp.provider_payment_id = str(paystack_id) if paystack_id is not None else None
    return True


# ----------------------------------------------------------------- events

async def _send_alerts(settings: Settings, alerts: list, now: datetime) -> None:
    for code, customer_id in alerts:
        await alert_owner(settings, code, customer_id, now)


async def _on_charge(db: Session, data: dict, settings: Settings, now: datetime, alerts: list) -> dict:
    ref = ps.event_reference(data)
    if not ref:
        logger.warning("subscription charge event without a reference ignored")
        return {"ok": True}
    bp = db.query(BillingPayment).filter_by(provider=PROVIDER, reference=ref).first()
    if bp is not None and bp.status != "pending":
        return {"ok": True, "duplicate": True}
    new_payment = None
    if bp is None:  # a renewal Paystack charged by itself: not issued by us, so it is tied to a customer by email
        customer = customer_for_event(db, data)
        if customer is None:
            alerts.append(("unknown_customer", None))
            return {"ok": True}
        new_payment = BillingPayment(customer_id=customer.id, provider=PROVIDER, reference=ref[:100],
                                     amount_minor=_expected_base(db, customer.id, settings), currency=ps.PLAN_CURRENCY, status="pending")
    else:
        customer = db.get(Customer, bp.customer_id)
        if customer is None:
            return {"ok": True}
    return await reconcile(db, customer, settings, now, alerts, hint_code=ps.event_subscription_code(data), new_payment=new_payment)


async def _on_subscription_event(db: Session, name: str, data: dict, settings: Settings, now: datetime, alerts: list) -> dict:
    customer = customer_for_event(db, data)
    if customer is None:
        alerts.append(("unknown_customer", None))
        return {"ok": True}
    result = await reconcile(db, customer, settings, now, alerts, hint_code=ps.event_subscription_code(data))
    if name == "invoice.payment_failed":
        alerts.append(("renewal_failed", customer.id))  # a missed period: nothing is extended
    return result


async def _on_refund(db: Session, data: dict, settings: Settings, now: datetime, alerts: list) -> dict:
    ref = ps.event_reference(data)
    bp = db.query(BillingPayment).filter_by(provider=PROVIDER, reference=ref).first() if ref else None
    if bp is None:
        alerts.append(("refund_unmatched", None))
        return {"ok": True}
    tx = await ps.fetch_transaction(ref, settings)
    if tx is not None and tx.get("status") == "reversed":  # UNVERIFIED wording; if it differs we only alert
        bp.status = "refunded"
    alerts.append(("refund_seen", bp.customer_id))
    return {"ok": True}  # access is never revoked automatically


async def handle_event(db: Session, event: dict, settings: Settings, now: datetime | None = None) -> dict | None:
    """The subscription half of the webhook. None = "not mine": the router then runs the existing handler unchanged."""
    name = ps.event_name(event)
    if name is None:
        return None
    data = ps.event_data(event)
    now = now or datetime.now(timezone.utc)
    alerts: list = []
    try:
        if name == "charge.success":
            try:
                mine = is_subscription_charge(db, data, settings)
            except Exception:  # noqa: BLE001 - never let the new path block a legacy payment
                logger.warning("could not classify a charge event; the existing handler will decide")
                return None
            if not mine:
                return None
            result = await _on_charge(db, data, settings, now, alerts)
        elif name in SUBSCRIPTION_EVENTS:
            result = await _on_subscription_event(db, name, data, settings, now, alerts)
        elif name.startswith("refund."):
            result = await _on_refund(db, data, settings, now, alerts)
        else:
            return None
    except RetryLater:
        await _send_alerts(settings, alerts, now)  # nothing was written before a RetryLater
        raise
    db.commit()  # release the write lock BEFORE any network wait for the alerts
    await _send_alerts(settings, alerts, now)
    return result
