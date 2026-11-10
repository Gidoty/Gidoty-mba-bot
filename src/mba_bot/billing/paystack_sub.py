"""Paystack subscription API access and tolerant parsers (designs/03, Stage 1).

Everything Paystack tells us about money comes through ONE seam, `get_json`, so tests can replace Paystack completely and
production has one place that talks to the network. Webhook bodies are only triggers; the facts come from these reads
(GET /transaction/verify/:reference, GET /subscription/:code ...). The parsers never raise on a missing or odd field:
they return None, and the caller treats None as "do not decide".

Verified against Paystack TEST mode (probe v3, designs/03 addendum 3): a create answers HTTP 201 (this module only
reads, but `initialize` in billing.py accepts 200 and 201); the subscriptions list is filtered by the NUMERIC plan id;
a cancelled subscription is `non-renewing` and keeps its next_payment_date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from ..config import Settings

PAYSTACK_API = "https://api.paystack.co"
PLAN_CURRENCY = "NGN"  # Paystack is Naira only on this account (verified, test mode)
READ_TIMEOUT_SECONDS = 8
# A monthly plan never has a next payment more than about 31 days out. This is a SANITY BOUND on what Paystack reports,
# not a date calculation: the period end is always Paystack's own next_payment_date.
MAX_PERIOD_DAYS = 45


class ProviderUnavailable(RuntimeError):
    """Paystack could not be asked or answered unreadably. Nothing was decided. The message holds no exchange text."""


async def get_json(path: str, settings: Settings) -> dict | None:
    """GET one Paystack path. Returns the decoded JSON object, or None for HTTP 404. Raises ProviderUnavailable otherwise."""
    if not settings.paystack_secret_key:
        raise ProviderUnavailable("not configured")
    try:
        async with httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS) as client:
            response = await client.get(PAYSTACK_API + path, headers={"Authorization": f"Bearer {settings.paystack_secret_key}"})
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


# ----------------------------------------------------------------- tolerant readers

def dig(value, *path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def as_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def as_str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_dt(value) -> datetime | None:
    """An ISO-8601 timestamp (Paystack sends a trailing Z) as an aware UTC datetime, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def same_email(a, b) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.strip().lower() == b.strip().lower() != ""


def event_name(event) -> str | None:
    return as_str(event.get("event")) if isinstance(event, dict) else None


def event_data(event) -> dict:
    data = event.get("data") if isinstance(event, dict) else None
    return data if isinstance(data, dict) else {}


def event_reference(data: dict) -> str | None:
    return as_str(data.get("reference")) or as_str(dig(data, "transaction", "reference")) or as_str(data.get("transaction_reference"))


def event_subscription_code(data: dict) -> str | None:
    return (as_str(data.get("subscription_code")) or as_str(dig(data, "subscription", "subscription_code"))
            or (as_str(data.get("code")) if str(data.get("code", "")).startswith("SUB_") else None))


def event_customer_email(data: dict) -> str | None:
    return as_str(dig(data, "customer", "email"))


def event_plan_code(data: dict) -> str | None:
    plan = data.get("plan")
    return as_str(plan) if isinstance(plan, str) else (as_str(dig(plan, "plan_code")) if isinstance(plan, dict) else None)


# ----------------------------------------------------------------- reads (tolerant)

async def fetch_transaction(reference: str, settings: Settings) -> dict | None:
    """Paystack's own record of a transaction (`data` of /transaction/verify), or None if it is not found or unusable."""
    body = await get_json(f"/transaction/verify/{quote(reference, safe='')}", settings)
    if not body or body.get("status") is not True or not isinstance(body.get("data"), dict):
        return None
    return body["data"]


async def fetch_subscription(code: str, settings: Settings) -> dict | None:
    body = await get_json(f"/subscription/{quote(code, safe='')}", settings)
    if not body or body.get("status") is not True or not isinstance(body.get("data"), dict):
        return None
    return body["data"]


async def fetch_plan_numeric_id(plan_code: str, settings: Settings) -> int | None:
    body = await get_json(f"/plan/{quote(plan_code, safe='')}", settings)
    return as_int(dig(body, "data", "id")) if body and body.get("status") is True else None


async def fetch_customer(email: str, settings: Settings) -> dict | None:
    body = await get_json(f"/customer/{quote(email, safe='')}", settings)
    if not body or body.get("status") is not True or not isinstance(body.get("data"), dict):
        return None
    return body["data"]


async def list_customer_subscriptions(plan_id: int, paystack_customer_id: int, settings: Settings) -> list[dict]:
    body = await get_json(f"/subscription?plan={plan_id}&customer={paystack_customer_id}&perPage=20", settings)
    items = body.get("data") if body and body.get("status") is True else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


# ----------------------------------------------------------------- reading a subscription

LIVE_STATUSES = frozenset({"active", "attention", "non-renewing"})


@dataclass(frozen=True)
class SubInfo:
    code: str | None
    status: str | None  # Paystack's word: active | attention | non-renewing | completed | cancelled | ...
    next_payment_date: datetime | None
    plan_code: str | None
    email: str | None
    email_token: str | None
    reusable: bool | None
    amount: int | None
    created_at: datetime | None


def read_subscription(data: dict) -> SubInfo:
    reusable = dig(data, "authorization", "reusable")
    return SubInfo(
        code=as_str(data.get("subscription_code")),
        status=as_str(data.get("status")),
        next_payment_date=parse_dt(data.get("next_payment_date")),
        plan_code=as_str(dig(data, "plan", "plan_code")),
        email=as_str(dig(data, "customer", "email")),
        email_token=as_str(data.get("email_token")),
        reusable=reusable if isinstance(reusable, bool) else None,
        amount=as_int(data.get("amount")),
        created_at=parse_dt(data.get("createdAt")),
    )


def our_status(paystack_status: str | None) -> str | None:
    """Paystack's subscription status as ours (Stage 0 states). None = unknown, so the caller changes nothing.
    attention = the last charge failed: still `active`, and the period end passing starts the grace window by itself."""
    return {
        "active": "active",
        "attention": "active",
        "non-renewing": "cancelled",
        "completed": "cancelled",
        "cancelled": "cancelled",
    }.get(paystack_status or "")
