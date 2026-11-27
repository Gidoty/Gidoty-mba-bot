"""NOWPayments (USDT on BSC), Stage 2 of designs/03. Runs ONLY while SUBSCRIPTION_BILLING_ENABLED and NOWPAYMENTS_ENABLED are on.

Decisions this module keeps (owner and reviewer, 10 to 12 Nov): USD 10 per month; the owner absorbs NOWPayments' fee (floating rate,
`is_fee_paid_by_user` false, 7-day invoice window); only a payment with status `finished` and at least the price (1 percent
tolerance, D8) grants access; `partially_paid` and every other status grant nothing; one invoice per month with a reminder and a manual
link; the NOWPayments Subscriptions API is NOT used; the owner is alerted, never an automatic refund or revoke.

How a payment is trusted:
  1. SIGNATURE over the raw bytes (header x-nowpayments-sig, HMAC-SHA512 with the IPN secret). Written against the PDF, which says:
     sort the keys, nested objects too, serialise, HMAC, compare the hex digest. The PDF does not say HOW the sender writes numbers
     or separators, so several equivalent serialisations are tried (see `signature_variants`). UNVERIFIED against a real IPN.
  2. The body is a TRIGGER. Only `payment_id` is taken from it. The payment is re-read from NOWPayments (GET /v1/payment/{id}) and
     that record decides. The shape of that record is NOT in the PDF section we read: every field is read defensively and the ones
     relied on are listed in `RELIED_ON`, so they can be checked against one real payment.
  3. The invoice is found by `order_id` (our opaque reference), never by email, address or amount. Price, currency and asset must match
     what was issued. Replay protection: payment_events key plus the unique billing_payments reference.
NOWPAYMENTS_IPN_MODE: off (ignore), observe (verify and log a masked verdict, WRITE NOTHING), enforce (apply). Nothing here places a
trade, touches a credential or sends money."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crypto_utils
from ..config import Settings
from ..models import BillingPayment, Customer, PaymentEvent, Subscription, SubscriptionToken
from . import notices
from . import paystack_sub as ps
from . import subscriptions as subs
from .entitlement import EntitlementState, _aware, entitlement_state, grace_end
from .paystack_sub import ProviderUnavailable
from .periods import add_months, to_utc

logger = logging.getLogger(__name__)

PROVIDER = "nowpayments"
API_BASE = "https://api.nowpayments.io"
PRICE_CURRENCY = "usd"
RENEWAL_WINDOW_DAYS = 7  # D10: crypto renewal opens this long before the period end
MAX_PENDING_PER_DAY = 3
OVERPAY_ALERT_FACTOR = Decimal("1.05")
READ_TIMEOUT_SECONDS = 8
SIGNATURE_HEADER = "x-nowpayments-sig"

PENDING_STATUSES = frozenset({"waiting", "confirming", "confirmed", "sending"})
KNOWN_STATUSES = PENDING_STATUSES | {"finished", "partially_paid", "failed", "expired", "refunded"}

# The fields of the GET /v1/payment/{id} answer this module relies on. NOT in the PDF section we read: UNVERIFIED until one real payment.
PAYMENT_WINDOW_MINUTES = 20  # how long a customer has to send one payment (the payment window NOWPayments shows; the customer's countdown is this long, from the moment the invoice was created)
SEEN_PREFIX = "seen:"  # outcome of a payment event written for a not-yet-final status (waiting, confirming, confirmed, sending)
TRANSFER_OUTCOMES = frozenset({"seen:confirming", "seen:confirmed", "seen:sending", "underpaid", "applied"})  # the payment was SEEN on the network (not merely created)
WRONG_COIN_OUTCOME = "wrong_coin"  # outcome of the one payment event that marks an invoice as having received a payment in another coin or network
COIN_LABELS = {"usdtbsc": "USDT on BSC (BEP20)"}  # what the customer is told to send; any other configured code is shown as its own name
RELIED_ON = ("payment_id", "payment_status", "order_id", "price_amount", "price_currency", "pay_currency", "pay_amount", "actually_paid")
OPTIONAL_FIELDS = ("parent_payment_id",)

subs.ALERTS.update({
    "crypto_underpaid": "Billing: a crypto payment was LESS than the price (or NOWPayments reported it partially paid). No access was granted.",
    "crypto_failed": "Billing: NOWPayments reported a crypto payment as failed. No access was granted.",
    "crypto_refunded": "Billing: NOWPayments reported a crypto payment as refunded. Nothing was revoked; please review it.",
    "crypto_overpaid": "Billing: a crypto payment was more than the price. One period was granted; the rest is for you to review. Nothing is refunded automatically.",
    "crypto_repeated_deposit": "Billing: a repeated or extra deposit to a crypto invoice was reported. Nothing was applied; please review it.",
    "crypto_duplicate_payment": "Billing: a second finished payment arrived for an invoice that was already paid. Nothing more was applied; please review it.",
    "crypto_payment_rejected": "Billing: a crypto payment failed a safety check (price, currency, asset or order). No access was granted.",
    "crypto_payment_unreadable": "Billing: NOWPayments' payment record could not be read as expected (a field is missing or odd). No access was granted; check the PR's field list against this payment.",
    "crypto_unknown_status": "Billing: NOWPayments reported a payment status we do not know. Nothing was changed.",
    "crypto_unknown_order": "Billing: a crypto payment notification matched no invoice we issued. Nothing was changed.",
    "crypto_paid_while_card_active": "Billing: a customer paid in crypto while their card subscription is still running. The payment is recorded, nothing was granted; please review it.",
    "crypto_wrong_asset": "Billing: a payment arrived in a different coin or network from the one the invoice asked for (NOWPayments converted it). It was NOT credited automatically and no access was granted; please look at it in the NOWPayments dashboard.",
    "crypto_below_minimum": "Billing: NOWPayments' minimum payment amount is above the price, so crypto checkout was refused.",
})


class BadSignature(Exception):
    """The IPN signature did not verify (enforce mode). The router answers 400 and nothing was read."""


class NotConfigured(Exception):
    """A required setting (API key, IPN secret, currency, IPN URL) is missing. The router answers 503."""


class CheckoutRefused(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


# ======================================================================== numbers and the signature

class _Num(str):
    """A JSON number kept as the exact text the sender wrote."""


def js_number_text(token: str) -> str:
    """How JavaScript's JSON.stringify would write this number: 10.0 -> 10, 1.50 -> 1.5, 1e-6 -> 0.000001, 1e-7 -> 1e-7, 1e21 -> 1e+21."""
    try:
        d = Decimal(token)
    except InvalidOperation:
        return token
    if not d.is_finite():
        return token
    if d == d.to_integral_value() and abs(d) < Decimal(10) ** 21:
        return str(int(d))
    f = float(token)
    if f == 0:
        return "0"
    sign = "-" if f < 0 else ""
    shortest = Decimal(repr(abs(f)))
    exp10 = shortest.adjusted()
    digits = "".join(str(x) for x in shortest.as_tuple().digits).rstrip("0") or "0"
    if -7 < exp10 < 21:
        plain = format(shortest, "f")
        if "." in plain:
            plain = plain.rstrip("0").rstrip(".")
        return sign + plain
    mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
    return f"{sign}{mantissa}e{'+' if exp10 > 0 else '-'}{abs(exp10)}"


def _parse_keeping_numbers(raw: bytes):
    return json.loads(raw.decode("utf-8"), parse_float=_Num, parse_int=_Num, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))


def _dump(obj, *, numbers: str, ascii_: bool, seps: tuple[str, str], sort_nested: bool, top: bool = True) -> str:
    item_sep, key_sep = seps
    if isinstance(obj, _Num):
        return js_number_text(obj) if numbers == "js" else str(obj)
    if isinstance(obj, dict):
        keys = sorted(obj) if (top or sort_nested) else list(obj)
        return "{" + item_sep.join(
            json.dumps(k, ensure_ascii=ascii_) + key_sep + _dump(obj[k], numbers=numbers, ascii_=ascii_, seps=seps, sort_nested=sort_nested, top=False)
            for k in keys) + "}"
    if isinstance(obj, list):
        return "[" + item_sep.join(_dump(v, numbers=numbers, ascii_=ascii_, seps=seps, sort_nested=sort_nested, top=False) for v in obj) + "]"
    return json.dumps(obj, ensure_ascii=ascii_)  # strings, true, false, null


def signature_variants(raw: bytes) -> list[tuple[str, bytes]]:
    """Every serialisation of the body the sender might have signed. All sort the top-level keys; they differ in whether nested keys are
    sorted (the PDF says yes; the common JavaScript idiom with a replacer array is different), the separators, whether non-ASCII is
    escaped, and how numbers are written (as received, or JavaScript style). 16 candidates. A forger without the secret cannot produce
    any of them, so trying several costs no security."""
    parsed = _parse_keeping_numbers(raw)
    out = []
    for sort_nested in (True, False):
        for seps_label, seps in (("compact", (",", ":")), ("spaced", (", ", ": "))):
            for ascii_ in (False, True):
                for numbers in ("orig", "js"):
                    label = f"{'nested-sorted' if sort_nested else 'top-sorted'}-{seps_label}-{'ascii' if ascii_ else 'utf8'}-{numbers}"
                    out.append((label, _dump(parsed, numbers=numbers, ascii_=ascii_, seps=seps, sort_nested=sort_nested).encode("utf-8")))
    return out


def verify_signature(secret: str | None, raw: bytes, header: str | None) -> str | None:
    """The label of the serialisation that matched, or None. Never raises on a malformed body."""
    if not secret or not header:
        return None
    try:
        given = header.strip().lower().encode("ascii")  # a hex digest is ASCII: anything else cannot match (and compare_digest raises on a non-ASCII str)
    except UnicodeEncodeError:
        return None
    try:
        variants = signature_variants(raw)
    except (ValueError, RecursionError):
        return None
    matched = None
    for label, body in variants:  # every variant is compared; no early exit
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest().encode("ascii")
        if hmac.compare_digest(expected, given) and matched is None:
            matched = label
    return matched


# ======================================================================== the payment record (defensive)

def to_decimal(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(repr(value)) if isinstance(value, float) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _id_text(value) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value.strip()):
        return value.strip()
    return None


@dataclass(frozen=True)
class Payment:
    payment_id: str | None
    status: str | None
    order_id: str | None
    price_amount: Decimal | None
    price_currency: str | None
    pay_currency: str | None
    pay_amount: Decimal | None
    actually_paid: Decimal | None
    parent_payment_id: str | None


def read_payment(body: dict | None) -> Payment:
    """Read GET /v1/payment/{id}. The shape is UNVERIFIED, so the object may be the body itself or sit under `data`; every field that
    is absent or odd becomes None and the caller treats None as "do not decide"."""
    d = body if isinstance(body, dict) else {}
    if "payment_status" not in d and isinstance(d.get("data"), dict):
        d = d["data"]

    def text(name):
        v = d.get(name)
        return v.strip().lower() if isinstance(v, str) and v.strip() else None

    order = d.get("order_id")
    return Payment(payment_id=_id_text(d.get("payment_id")), status=text("payment_status"),
                   order_id=order.strip() if isinstance(order, str) and order.strip() else None,
                   price_amount=to_decimal(d.get("price_amount")), price_currency=text("price_currency"), pay_currency=text("pay_currency"),
                   pay_amount=to_decimal(d.get("pay_amount")), actually_paid=to_decimal(d.get("actually_paid")),
                   parent_payment_id=_id_text(d.get("parent_payment_id")))


def missing_fields(p: Payment) -> list[str]:
    names = {"payment_id": p.payment_id, "payment_status": p.status, "order_id": p.order_id, "price_amount": p.price_amount,
             "price_currency": p.price_currency, "pay_currency": p.pay_currency, "pay_amount": p.pay_amount, "actually_paid": p.actually_paid}
    return [k for k in RELIED_ON if names[k] is None]


@dataclass(frozen=True)
class Verdict:
    kind: str  # pending | apply | underpaid | failed | expired | refunded | reject | unreadable | unknown_status | repeated
    reason: str = "-"
    usd_value: Decimal | None = None
    overpaid: bool = False


def expected_usd(bp: BillingPayment) -> Decimal:
    return Decimal(bp.amount_minor) / Decimal(100)


def judge(p: Payment, bp: BillingPayment, settings: Settings) -> Verdict:
    """The pure ladder. Reads only the record, the issued row and the settings."""
    miss = [m for m in missing_fields(p) if m not in ("pay_amount", "actually_paid")]
    if miss:
        return Verdict("unreadable", "missing:" + ",".join(miss))
    if p.order_id != bp.reference:
        return Verdict("reject", "order_mismatch")
    price = expected_usd(bp)
    if p.price_currency != PRICE_CURRENCY:
        return Verdict("reject", "price_currency_mismatch")
    if p.price_amount != price:
        return Verdict("reject", "price_mismatch")
    if p.pay_currency != (settings.nowpayments_pay_currency or ""):
        return Verdict("reject", "wrong_asset")
    if p.parent_payment_id:
        return Verdict("repeated", "parent_payment_id")
    status = p.status
    if status in PENDING_STATUSES:
        return Verdict("pending", status)
    if status == "partially_paid":
        usd = p.price_amount * p.actually_paid / p.pay_amount if p.pay_amount and p.pay_amount > 0 and p.actually_paid is not None else None
        return Verdict("underpaid", "partially_paid", usd)  # the value (when the record has the amounts) lets the customer's page say how much is short
    if status in ("failed", "expired", "refunded"):
        return Verdict(status, status)
    if status != "finished":
        return Verdict("unknown_status", status or "-")
    if p.pay_amount is None or p.actually_paid is None or p.pay_amount <= 0 or p.actually_paid < 0:
        return Verdict("unreadable", "missing:pay_amount,actually_paid" if p.pay_amount is None or p.actually_paid is None else "amount_values")
    usd_value = p.price_amount * p.actually_paid / p.pay_amount  # the invoice's own rate: no live rate is needed
    tolerance = Decimal(str(settings.nowpayments_underpay_tolerance_pct)) / Decimal(100)
    if usd_value < price * (1 - tolerance):
        return Verdict("underpaid", "below_tolerance", usd_value)
    return Verdict("apply", "-", usd_value, overpaid=usd_value > price * OVERPAY_ALERT_FACTOR)


# ======================================================================== the network (three allowlisted calls)

_GET_PAYMENT = re.compile(r"^/v1/payment/[0-9]{1,20}$")
_CODE = re.compile(r"^[a-z0-9_]{2,24}$")


async def get_json(path: str, settings: Settings) -> dict | None:
    """GET /v1/payment/{id}. Returns the decoded JSON object, None for HTTP 404, raises ProviderUnavailable otherwise."""
    if not _GET_PAYMENT.match(path):
        raise ProviderUnavailable("blocked path")
    if not settings.nowpayments_api_key:
        raise ProviderUnavailable("not configured")
    try:
        async with httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS) as client:
            response = await client.get(API_BASE + path, headers={"x-api-key": settings.nowpayments_api_key})
    except httpx.HTTPError as exc:
        raise ProviderUnavailable(type(exc).__name__) from None
    if response.status_code == 404:
        return None
    if response.status_code >= 500 or response.status_code in (401, 403, 429):
        raise ProviderUnavailable(f"http {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise ProviderUnavailable("unreadable response") from None
    if not isinstance(body, dict):
        raise ProviderUnavailable("unexpected response")
    return body


def post_invoice(settings: Settings, body: dict) -> tuple[int, dict | None]:
    """POST /v1/invoice, the one write this module sends. Synchronous (the route is). Raises ProviderUnavailable if NOWPayments cannot be reached."""
    try:
        response = httpx.post(API_BASE + "/v1/invoice", json=body, headers={"x-api-key": settings.nowpayments_api_key or ""}, timeout=15)
    except httpx.HTTPError as exc:
        raise ProviderUnavailable(type(exc).__name__) from None
    try:
        data = response.json()
    except ValueError:
        data = None
    return response.status_code, data if isinstance(data, dict) else None


def get_min_amount(settings: Settings) -> Decimal | None:
    """The minimum payment in USD terms (`fiat_equivalent`) for the configured asset, or None if it cannot be read. Never raises: an
    unreadable minimum must not block checkout (a refusal by NOWPayments is handled where the invoice is created)."""
    code = settings.nowpayments_pay_currency or ""
    if not _CODE.match(code) or not settings.nowpayments_api_key:
        return None
    try:
        response = httpx.get(f"{API_BASE}/v1/min-amount?currency_from={code}&currency_to={code}&fiat_equivalent=usd",
                             headers={"x-api-key": settings.nowpayments_api_key}, timeout=8)
        data = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return to_decimal(data.get("fiat_equivalent")) if isinstance(data, dict) else None


# ======================================================================== checkout (POST /api/billing/crypto/initialize)

def new_reference() -> str:
    return f"mbacrypto_{secrets.token_hex(12)}"


def require_configured(settings: Settings, *, quiet: bool = False) -> None:
    if not (settings.nowpayments_api_key and settings.nowpayments_pay_currency and settings.nowpayments_ipn_url
            and _CODE.match(settings.nowpayments_pay_currency)):
        raise NotConfigured("NOWPayments is not configured")
    if price_problem(settings):
        # Fail closed. An operator-facing line says exactly what is wrong; the customer only sees that crypto payment is not available.
        if not quiet:  # the options route asks on every dashboard load: only a real checkout attempt logs the operator line
            logger.error("nowpayments: NOWPAYMENTS_PRICE_USD is malformed, zero or negative; refusing to create an invoice (fix the value and restart the service)")
        raise NotConfigured("NOWPayments price is not valid")


def price_problem(settings: Settings) -> bool:
    """True when NOWPAYMENTS_PRICE_USD was set to something that is not a positive amount of dollars (config turns that into an empty string). Unset is the default price, not a problem."""
    return not settings.nowpayments_price_usd


def price_cents(settings: Settings) -> int:
    if price_problem(settings):
        raise NotConfigured("NOWPayments price is not valid")  # never a guessed price
    return int((Decimal(settings.nowpayments_price_usd) * 100).to_integral_value())


def checkout_refusal(customer: Customer, settings: Settings, now: datetime) -> str | None:
    """Why this customer may not start a crypto invoice now, or None. Allowed: no subscription, grace, expired, or a period ending within
    7 days. Refused: lifetime access, a running card subscription (it renews by itself: one channel at a time), too early."""
    state = entitlement_state(customer, settings=settings, now=now)  # the one place that knows about lifetime access (has_paid is read nowhere else)
    if state == EntitlementState.LEGACY_LIFETIME:
        return "this customer already has lifetime access"
    row = customer.subscription
    if row is None:
        return None
    end = _aware(row.current_period_end)
    if row.provider == subs.PROVIDER and row.status == "active" and end is not None and now < end:
        return "your card subscription renews by itself"
    if state in (EntitlementState.ACTIVE, EntitlementState.CANCELLED) and end is not None and end - now > timedelta(days=RENEWAL_WINDOW_DAYS):
        return "your paid period is still running; renewal opens 7 days before it ends"
    return None


def valid_invoice_url(url) -> bool:
    if not isinstance(url, str):
        return False
    u = urlparse(url)
    return u.scheme == "https" and bool(u.hostname) and (u.hostname == "nowpayments.io" or u.hostname.endswith(".nowpayments.io"))


def start_checkout(db: Session, customer: Customer, settings: Settings, now: datetime | None = None) -> str:
    """Create ONE invoice and return its link. Raises CheckoutRefused (with the HTTP status) or NotConfigured."""
    now = now or datetime.now(timezone.utc)
    require_configured(settings)
    reason = checkout_refusal(customer, settings, now)
    if reason:
        raise CheckoutRefused(409, reason)
    pending = db.query(BillingPayment).filter(BillingPayment.provider == PROVIDER, BillingPayment.customer_id == customer.id,
                                              BillingPayment.status == "pending", BillingPayment.issued_at > now - timedelta(hours=24)).count()
    if pending >= MAX_PENDING_PER_DAY:
        raise CheckoutRefused(409, "too many unpaid invoices; pay one of them or try again tomorrow")
    cents = price_cents(settings)
    logger.info("nowpayments: creating an invoice for price_usd=%s (NOWPAYMENTS_PRICE_USD as this process read it)", Decimal(cents) / 100)
    minimum = get_min_amount(settings)
    if minimum is not None and minimum > Decimal(cents) / 100:
        logger.warning("nowpayments minimum above the price: checkout refused")
        raise CheckoutRefused(409, "crypto payment is not available right now, please pay by card")

    reference = new_reference()
    row = BillingPayment(customer_id=customer.id, provider=PROVIDER, reference=reference, amount_minor=cents, currency="USD",
                         usd_value=Decimal(cents) / 100, status="pending")
    db.add(row)
    db.flush()
    db.commit()  # the invoice row exists BEFORE the link does (so a payment can always be matched) and no write lock is held during the network call
    dash = notices.dashboard_link(settings)
    body = {"price_amount": float(Decimal(cents) / 100), "price_currency": PRICE_CURRENCY, "pay_currency": settings.nowpayments_pay_currency,
            "order_id": reference, "order_description": "Metabridge subscription, one month", "ipn_callback_url": settings.nowpayments_ipn_url,
            "success_url": dash, "cancel_url": dash, "is_fee_paid_by_user": False, "is_fixed_rate": False}
    try:
        status_code, data = post_invoice(settings, body)
    except ProviderUnavailable:
        row.status = "failed"
        db.commit()  # the route raises next, which rolls the session back: record the failure first
        raise CheckoutRefused(502, "could not start the payment, please try again") from None
    code = data.get("code") if isinstance(data, dict) else None
    # NOWPayments, like Paystack, may answer a successful create with 201: both are success.
    if status_code not in (200, 201) or data is None or not valid_invoice_url(data.get("invoice_url")):
        row.status = "failed"
        db.commit()
        logger.error("nowpayments invoice refused: http %s code %s", status_code, code if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,40}", code) else "-")
        raise CheckoutRefused(502, "could not start the payment, please try again")
    return data["invoice_url"]


# ======================================================================== the IPN

def _log_verdict(mode: str, sig: str | None, p: Payment | None, bp: BillingPayment | None, v: Verdict | None, note: str = "") -> None:
    """A masked verdict, for the owner to compare with a real payment. No body, no secret, no address, no link, no e-mail."""
    logger.info("nowpayments ipn[%s]: sig=%s status=%s payment=%s order=%s asset=%s paid=%s of=%s usd=%s verdict=%s reason=%s fields_missing=%s %s", mode,
                sig or "INVALID", p.status if p else "-", subs._tail(p.payment_id) if p else "-", subs._tail(bp.reference if bp else (p.order_id if p else None)),
                p.pay_currency if p else "-", p.actually_paid if p else "-", p.pay_amount if p else "-", v.usd_value if v and v.usd_value is not None else "-",
                v.kind if v else "-", v.reason if v else "-", (",".join(missing_fields(p)) or "-") if p else "-", note)


def _payment_id_from_trigger(raw: bytes) -> tuple[str | None, str | None]:
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    order = body.get("order_id")
    return _id_text(body.get("payment_id")), order if isinstance(order, str) else None


def _notification_facts(raw: bytes) -> dict:
    """The only things taken from a VERIFIED notification when its payment cannot be read back: which invoice it names, which payment it is a child of, and the coin it says it is in. Never an amount."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}
    if not isinstance(body, dict):
        return {}
    order, cur, st = body.get("order_id"), body.get("pay_currency"), body.get("payment_status")
    return {"payment_id": _id_text(body.get("payment_id")), "order_id": order.strip() if isinstance(order, str) and order.strip() else None, "parent_payment_id": _id_text(body.get("parent_payment_id")),
            "status": st.strip().lower() if isinstance(st, str) else None, "pay_currency": cur.strip().lower() if isinstance(cur, str) and cur.strip() else None}


def find_invoice(db: Session, facts: dict) -> BillingPayment | None:
    """Our invoice row for a notification, from the notification's own order_id, else from the parent payment id it names (the payment we stored for the invoice, or remembered in this process)."""
    if facts.get("order_id"):
        bp = db.query(BillingPayment).filter_by(provider=PROVIDER, reference=facts["order_id"]).first()
        if bp is not None:
            return bp
    parent = facts.get("parent_payment_id")
    if parent:
        bp = db.query(BillingPayment).filter_by(provider=PROVIDER, provider_payment_id=parent).first()
        if bp is not None:
            return bp
        ref = next((r for r, (pid, _s) in _SEEN.items() if pid == parent), None)
        if ref:
            return db.query(BillingPayment).filter_by(provider=PROVIDER, reference=ref).first()
    return None


async def wrong_coin(db: Session, bp: BillingPayment, mode: str, settings: Settings, now: datetime, *, source: str) -> dict:
    """A payment for this invoice arrived in a coin or network the invoice did not ask for (NOWPayments converted it, or it could not be read with our key). It is NEVER credited. observe: nothing is written, the
    invoice is remembered as such in this process's memory only. enforce: one `payment_events` row (key per invoice, so a second notification changes nothing) and, the first time only, one owner alert with a masked order tail."""
    _WRONG.add(bp.reference)
    logger.info("nowpayments wrong coin or network: mode=%s order=%s source=%s", mode, subs._tail(bp.reference), source)
    if mode != "enforce":
        return {"ok": True, "observed": "wrong_coin"}
    subs._begin_write(db)
    first = _record_event(db, f"nowpayments:wrongcoin:{bp.reference}", bp, WRONG_COIN_OUTCOME, None)
    db.commit()
    if not first:
        return {"ok": True, "duplicate": True}
    await subs.alert_owner(settings, "crypto_wrong_asset", bp.customer_id, now, detail=f" Order {subs._tail(bp.reference)}", dedupe=f":{bp.reference}")
    return {"ok": True, "recorded": "wrong_coin"}


async def handle_ipn(db: Session, raw: bytes, header: str | None, settings: Settings, now: datetime | None = None) -> dict:
    """The IPN. Returns the JSON answer. Raises BadSignature (400), NotConfigured or ProviderUnavailable or subs.RetryLater (503)."""
    now = now or datetime.now(timezone.utc)
    mode = settings.nowpayments_ipn_mode
    if mode == "off":
        logger.info("nowpayments ipn ignored: NOWPAYMENTS_IPN_MODE is off")
        return {"ok": True, "ignored": "mode_off"}
    if not settings.nowpayments_ipn_secret:
        logger.warning("nowpayments ipn: the IPN secret is not configured")
        if mode == "enforce":
            raise NotConfigured("IPN secret not configured")
        return {"ok": True, "observed": "no_secret"}
    sig = verify_signature(settings.nowpayments_ipn_secret, raw, header)
    if sig is None:
        _log_verdict(mode, None, None, None, None, "signature did not verify under any of 16 serialisations")
        if mode == "enforce":
            raise BadSignature()
        return {"ok": True, "observed": "bad_signature"}  # observe: nothing is read, nothing is changed
    payment_id, callback_order = _payment_id_from_trigger(raw)
    if payment_id is None:
        _log_verdict(mode, sig, None, None, None, "no payment_id in the notification")
        return {"ok": True, "ignored": "no_payment_id"}
    try:
        record = await get_json(f"/v1/payment/{payment_id}", settings)
    except ProviderUnavailable as exc:
        logger.warning("nowpayments payment could not be read (%s)", exc)
        if mode == "enforce":
            raise
        return {"ok": True, "observed": "provider_unavailable"}
    if record is None:
        _log_verdict(mode, sig, None, None, None, "payment not found at NOWPayments")
        facts = _notification_facts(raw)
        found = find_invoice(db, facts)
        coin = settings.nowpayments_pay_currency or ""
        other_coin = facts.get("pay_currency") is not None and facts["pay_currency"] != coin
        if found is not None and (other_coin or facts.get("parent_payment_id")):  # a child payment (a conversion) cannot be read with our key: say so on the invoice, grant nothing
            return await wrong_coin(db, found, mode, settings, now, source="unreadable_payment")
        return {"ok": True, "ignored": "payment_not_found"}
    return await process_record(db, record, settings, now, mode=mode, sig=sig, callback_order=callback_order)


_SEEN: dict[str, tuple[str, str]] = {}  # reference -> (latest payment id, its status), kept in THIS process only (a restart clears it); lets the customer's "I have sent the payment" button work in observe mode, where nothing is written
_SEEN_MAX = 500


def remember_seen(reference: str, payment_id: str | None, status: str | None) -> None:
    if not reference or not payment_id:
        return
    if len(_SEEN) >= _SEEN_MAX and reference not in _SEEN:
        _SEEN.pop(next(iter(_SEEN)))
    _SEEN[reference] = (payment_id, status or "-")


def seen_payment_id(reference: str) -> str | None:
    hit = _SEEN.get(reference)
    return hit[0] if hit else None


_WRONG: set[str] = set()  # invoice references that received a wrong-coin payment, in this process's memory (what observe mode can show without a write)
_AMOUNT: dict[str, str] = {}  # reference -> the exact amount of the coin the latest payment asked for, in memory (observe mode)


def wrong_coin_seen(reference: str) -> bool:
    return reference in _WRONG


def reset_seen_for_tests() -> None:
    _SEEN.clear()
    _WRONG.clear()
    _AMOUNT.clear()


async def process_record(db: Session, record: dict, settings: Settings, now: datetime, *, mode: str, sig: str | None, callback_order: str | None = None, expect_customer_id: str | None = None, detail: dict | None = None) -> dict:
    """THE ONE APPLY RULE, from a payment record read back from NOWPayments to the answer. The IPN calls it with a signed notification's payment; the customer's "I have sent the payment" check calls it with a payment
    the server asked NOWPayments about itself. observe: verdict logged, nothing written. enforce: the same ladder, the same writes, the same alerts. `expect_customer_id`, when given, makes a record that belongs to
    another customer's invoice act as an unknown order (nothing is read out of it and nothing is changed). `detail`, when given, is filled with the payment and the verdict for the caller's own answer."""
    p = read_payment(record)
    bp = db.query(BillingPayment).filter_by(provider=PROVIDER, reference=p.order_id).first() if p.order_id else None
    if bp is not None and expect_customer_id is not None and bp.customer_id != expect_customer_id:
        _log_verdict(mode, sig, p, None, None, "the payment belongs to another customer's invoice")
        return {"ok": True, "ignored": "unknown_order"}
    if bp is None:
        _log_verdict(mode, sig, p, None, None, "no invoice of ours has this order_id")
        if mode == "enforce":
            out = subs.Out()
            out.append(("crypto_unknown_order", None))
            await subs._send_alerts(settings, out, now)
        return {"ok": True, "ignored": "unknown_order"}
    verdict = judge(p, bp, settings)
    if callback_order is not None and p.order_id is not None and callback_order != p.order_id:
        verdict = Verdict("reject", "callback_order_differs")  # the notification and the record disagree: believe neither
    _log_verdict(mode, sig, p, bp, verdict)
    if detail is not None:
        detail.update(payment=p, verdict=verdict, invoice_reference=bp.reference, invoice_cents=bp.amount_minor)
    if verdict.kind == "reject" and verdict.reason == "wrong_asset":
        return await wrong_coin(db, bp, mode, settings, now, source="record_in_another_coin")
    if verdict.kind in ("pending", "underpaid", "apply", "failed", "expired", "refunded"):
        remember_seen(bp.reference, p.payment_id, p.status)  # in memory only: no database write, so observe mode stays write-free
        if verdict.kind == "pending" and p.pay_amount is not None:
            _AMOUNT[bp.reference] = format(p.pay_amount.normalize(), "f")
    if mode == "observe":
        return {"ok": True, "observed": verdict.kind}

    # ---- enforce: reads first (no write pending), then one short synchronous write stretch, then alerts
    out = subs.Out()
    old_sub_code = None
    if verdict.kind == "apply":
        customer = db.get(Customer, bp.customer_id)
        row = customer.subscription if customer is not None else None
        end = _aware(row.current_period_end) if row is not None else None
        running_card = row is not None and row.provider == subs.PROVIDER and row.status == "active" and end is not None and now < end
        if row is not None and row.provider == subs.PROVIDER and row.provider_subscription_ref and not running_card:
            try:
                old = await ps.fetch_subscription(row.provider_subscription_ref, settings)
            except ProviderUnavailable:
                old = None
                out.append(("old_subscription_not_disabled", bp.customer_id))  # Paystack could not be asked: do not hold the crypto payment back
            old_next = ps.parse_dt(old.get("next_payment_date")) if old else None
            if old and (old.get("status") == "attention" or (old.get("status") == "active" and old_next is not None and old_next <= now)):
                old_sub_code = row.provider_subscription_ref
    result = _apply(db, bp, p, verdict, settings, now, out, old_sub_code)
    db.commit()  # release the write lock before any network wait
    await subs._send_alerts(settings, out, now)
    for code, token, customer_id in out.disable:
        if not await subs._disable_superseded(settings, code, token):
            await subs.alert_owner(settings, "old_subscription_not_disabled", customer_id, now)
    return result


def _record_event(db: Session, key: str, bp: BillingPayment, outcome: str, usd: Decimal | None, *, coin_amount: Decimal | None = None, coin: str | None = None) -> bool:
    """False if this exact notification was already recorded (replay). `coin_amount` (with `coin`) is the exact amount of the coin the payment asked for, kept in millionths in the same column (its currency column then names the coin)."""
    try:
        with db.begin_nested():
            amount = int((usd * 100).to_integral_value()) if usd is not None else (int((coin_amount * 1_000_000).to_integral_value()) if coin_amount is not None else None)
            db.add(PaymentEvent(provider=PROVIDER, event_key=key[:160], reference=bp.reference, customer_id=bp.customer_id,
                                amount=amount, currency=(coin or "USD")[:8], outcome=outcome[:24]))
            db.flush()
    except IntegrityError:
        return False
    return True


def _anchor(row: Subscription | None, settings: Settings, now: datetime) -> datetime:
    """D9: the period end if the customer pays before it or inside the grace window; the moment of applying otherwise.
    (Anchoring on 'the later of now and the end' would let a customer who always pays on day 5 of the grace drift later every month.)"""
    if row is None:
        return now
    end = _aware(row.current_period_end)
    if end is None:
        return now
    gend = grace_end(row, settings.subscription_grace_days)
    if now < end or (gend is not None and now < gend):
        return end
    return now


def _apply(db: Session, bp: BillingPayment, p: Payment, v: Verdict, settings: Settings, now: datetime, out: subs.Out, old_sub_code: str | None) -> dict:
    status = p.status or "-"
    key = f"nowpayments:{p.payment_id}:{status}"
    if v.kind == "pending":
        if bp.status == "pending":  # a settled invoice is not touched; this only lets the customer's page show "transfer seen and confirming"
            subs._begin_write(db)
            bp.provider_payment_id = p.payment_id  # the latest payment NOWPayments made for this invoice (a new one is made when a payment window runs out)
            _record_event(db, key, bp, (SEEN_PREFIX + status)[:24], None, coin_amount=p.pay_amount, coin=p.pay_currency)
        return {"ok": True, "pending": status}
    if v.kind == "unreadable":
        out.append(("crypto_payment_unreadable", bp.customer_id))
        return {"ok": True, "rejected": "unreadable"}
    if v.kind == "reject":
        out.append(("crypto_payment_rejected", bp.customer_id))
        return {"ok": True, "rejected": v.reason}
    if v.kind == "unknown_status":
        out.append(("crypto_unknown_status", bp.customer_id))
        return {"ok": True, "ignored": "unknown_status"}
    if v.kind == "repeated":
        out.append(("crypto_repeated_deposit", bp.customer_id))
        return {"ok": True, "rejected": "repeated_deposit"}

    subs._begin_write(db)
    if v.kind in ("underpaid", "failed", "expired", "refunded"):
        if not _record_event(db, key, bp, v.kind, v.usd_value):
            return {"ok": True, "duplicate": True}
        if bp.status != "paid" or v.kind == "refunded":
            bp.status = v.kind
        if v.usd_value is not None:
            bp.usd_value = v.usd_value
        if v.kind == "underpaid":
            out.append(("crypto_underpaid", bp.customer_id))
        elif v.kind == "failed":
            out.append(("crypto_failed", bp.customer_id))
        elif v.kind == "refunded":
            out.append(("crypto_refunded", bp.customer_id))  # recorded and alerted; access is never revoked automatically
        return {"ok": True, "recorded": v.kind}

    # ---- v.kind == "apply": a finished payment of at least the price
    if bp.status == "paid":
        if bp.provider_payment_id == p.payment_id:
            return {"ok": True, "duplicate": True}
        out.append(("crypto_duplicate_payment", bp.customer_id))
        return {"ok": True, "rejected": "already_paid"}
    if not _record_event(db, key, bp, "applied", v.usd_value):
        return {"ok": True, "duplicate": True}
    customer = db.get(Customer, bp.customer_id)
    row = customer.subscription if customer is not None else None
    bp.status, bp.paid_at, bp.provider_payment_id, bp.usd_value = "paid", now, p.payment_id, v.usd_value
    if v.overpaid:
        out.append(("crypto_overpaid", bp.customer_id))
    if row is not None and row.provider == subs.PROVIDER and row.status == "active" and _aware(row.current_period_end) and now < _aware(row.current_period_end):
        out.append(("crypto_paid_while_card_active", bp.customer_id))  # recorded, not applied: two channels must not run at once
        return {"ok": True, "recorded": "paid_not_applied"}

    start = _anchor(row, settings, now)
    end = add_months(start, 1)
    if row is None:
        row = Subscription(customer_id=bp.customer_id, status="active", current_period_start=start, current_period_end=end, provider=PROVIDER)
        db.add(row)
        if customer is not None and customer.account is not None:
            customer.account.is_active = True  # as the existing activation flow does on a first payment
    else:
        if row.provider == subs.PROVIDER and old_sub_code:
            tok = db.query(SubscriptionToken).filter_by(subscription_id=row.id).first()
            try:
                token = crypto_utils.decrypt(tok.token_enc, key=settings.encryption_key) if tok is not None and settings.encryption_key else None
            except crypto_utils.DecryptionFailed:
                token = None
            if token:
                out.disable.append((old_sub_code, token, bp.customer_id))
            else:
                out.append(("old_subscription_not_disabled", bp.customer_id))
        row.status, row.current_period_start, row.current_period_end = "active", start, end
        row.provider, row.provider_subscription_ref, row.grace_until = PROVIDER, None, None
    bp.period_start, bp.period_end = start, end
    return {"ok": True, "applied": True}
