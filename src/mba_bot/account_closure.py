"""Completing an approved account deletion request (docs/safety/statements_and_closure.md). Called only from the admin dashboard, behind ADMIN_ACTIONS_ENABLED, ACCOUNT_DELETION_ENABLED, the admin sign-in and a fresh
authenticator code. It changes the database only: it places, closes or modifies no trade and no exchange order, and calls no exchange or payment provider.

What it does, in one transaction: erases the customer's personal data and anonymises the customer row. What it KEEPS, and why, is `KEPT` below; the Privacy Policy lists the same records (`privacy.RETAINED_RECORDS`)
and a test checks the two agree. Rules, all tested: the request must be a deletion that an admin already approved; no trade of the account may still be open (a live position is never abandoned); an admin's own
account cannot be deleted; the admin must type the exact confirmation; the table list is derived from the schema, so a table added later that holds a customer or account id is erased by default and a table kept
for legal reasons must be named here on purpose."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .api.security import hash_password
from .models import AdminChange, Base, Customer, DataRequest, Subscription, Trade, TradingAccount, NON_TERMINAL_TRADE_STATUSES

# Tables that keep their rows (they reference the customer id, which stays as an anonymised shell). Each maps to a line of privacy.RETAINED_RECORDS.
KEPT = {
    "billing_payments": "payment and subscription records",
    "payment_events": "payment and subscription records",
    "payment_references": "payment and subscription records",
    "subscriptions": "payment and subscription records",
    "billing_notices": "payment and subscription records",
    "legal_acceptances": "your acceptances of the Terms, Privacy Policy and Risk Disclosure, with version and time",
    "data_requests": "the record of your request and its outcome",
}
# Tables that are not about the deleted customer at all (admins, operator state): never touched.
UNRELATED = frozenset({"admin_audit", "admin_changes", "admin_totp", "kill_switch", "kill_switch_events", "operator_settings"})
SHELL = "customers"  # anonymised, not deleted
VIA_SUBSCRIPTION = frozenset({"subscription_tokens"})  # the provider's email token is a secret: erased even though the subscription row is kept
VIA_TRADE = frozenset({"trade_telemetry"})


class ClosureError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status_code, message


def confirmation_text(customer_id: str) -> str:
    return f"DELETE {customer_id[:8]}"


def classify() -> dict[str, str]:
    """Every table in the schema with what happens to it: kept, shell, unrelated, or erased. Derived from the columns, so nothing is forgotten."""
    out = {}
    for t in Base.metadata.sorted_tables:
        cols = {c.name for c in t.columns}
        if t.name in KEPT:
            out[t.name] = "kept"
        elif t.name == SHELL:
            out[t.name] = "shell"
        elif t.name in UNRELATED:
            out[t.name] = "unrelated"
        elif cols & {"customer_id", "account_id"} or t.name in VIA_SUBSCRIPTION or t.name in VIA_TRADE or t.name == "trading_accounts":
            out[t.name] = "erased"
        else:
            out[t.name] = "unrelated"
    return out


def _ids(db: Session, customer: Customer) -> dict:
    acct = db.scalars(select(TradingAccount).where(TradingAccount.customer_id == customer.id)).first()
    acct_id = acct.id if acct else None
    trade_ids = [r for r in db.scalars(select(Trade.id).where(Trade.account_id == acct_id))] if acct_id else []
    sub_ids = list(db.scalars(select(Subscription.id).where(Subscription.customer_id == customer.id)))
    return {"account_id": acct_id, "trade_ids": trade_ids, "subscription_ids": sub_ids}


def complete_deletion(db: Session, admin_id: str, request_id: str, reason: str, confirm: str, *, admin_emails=(), now: datetime | None = None) -> dict:
    """Erase and anonymise. Returns {"erased": {table: rows}, "kept": [...], "warnings": [...]}; raises ClosureError before changing anything when a rule is not met."""
    now = now or datetime.now(timezone.utc)
    text = " ".join(str(reason or "").split())
    if not 5 <= len(text) <= 200:
        raise ClosureError(422, "Give a reason of 5 to 200 characters.")
    req = db.get(DataRequest, request_id)
    if req is None:
        raise ClosureError(404, "no such request")
    if req.kind != "deletion":
        raise ClosureError(409, "this is not a deletion request")
    if req.status != "approved":
        raise ClosureError(409, f"the request is {req.status}; approve it first" if req.status == "open" else f"the request is already {req.status}")
    customer = db.get(Customer, req.customer_id)
    if customer is None:
        raise ClosureError(404, "the customer no longer exists")
    if (customer.email or "").strip().lower() in set(admin_emails or ()):
        raise ClosureError(409, "an admin account cannot be deleted from here")
    if confirm != confirmation_text(customer.id):
        raise ClosureError(422, f'Type the confirmation exactly: {confirmation_text(customer.id)}')
    ids = _ids(db, customer)
    if ids["account_id"] and db.scalar(select(func.count()).select_from(Trade).where(Trade.account_id == ids["account_id"], Trade.status.in_(NON_TERMINAL_TRADE_STATUSES))):
        raise ClosureError(409, "a trade of this account is still open; it must close first")
    warnings = []
    sub = db.scalars(select(Subscription).where(Subscription.customer_id == customer.id)).first()
    if sub is not None and sub.status == "active":
        warnings.append("the subscription row is still active: cancel it at the payment provider yourself; this tool does not call the provider")
    erased: dict[str, int] = {}
    for t in reversed(Base.metadata.sorted_tables):
        how = classify()[t.name]
        if how != "erased":
            continue
        cols = {c.name for c in t.columns}
        if t.name == "trading_accounts":
            cond = t.c.customer_id == customer.id
        elif "account_id" in cols:
            if not ids["account_id"]:
                continue
            cond = t.c.account_id == ids["account_id"]
        elif t.name in VIA_SUBSCRIPTION:
            if not ids["subscription_ids"]:
                continue
            cond = t.c.subscription_id.in_(ids["subscription_ids"])
        elif t.name in VIA_TRADE:
            if not ids["trade_ids"]:
                continue
            cond = t.c.trade_id.in_(ids["trade_ids"])
        elif "customer_id" in cols:
            cond = t.c.customer_id == customer.id
        else:
            continue
        n = db.execute(delete(t).where(cond)).rowcount
        if n:
            erased[t.name] = n
    db.execute(update(Customer).where(Customer.id == customer.id).values(email=f"deleted-{customer.id[:8]}@deleted.invalid", password_hash=hash_password(secrets.token_hex(32)), is_active=False))
    db.expire_all()
    req = db.get(DataRequest, request_id)
    req.status, req.handled_at = "done", now
    db.add(AdminChange(at=now, admin_id=admin_id, kind="action", action="complete_deletion", target=customer.id, old="approved", new=f"done, {sum(erased.values())} rows erased in {len(erased)} tables", reason=text))
    return {"erased": erased, "kept": sorted(k for k, v in classify().items() if v == "kept"), "warnings": warnings}
