"""Hourly recovery of a Paystack renewal whose webhook never arrived (or never settled). Runs ONLY with SUBSCRIPTION_BILLING_ENABLED and PAYSTACK_RENEWAL_RECONCILE both on (the second is OFF by default: this job
makes read-only calls with the live Paystack key, which the operator enables by hand).

What it does, for each Paystack subscription that is `active` in our table and whose paid period has ENDED (within the last RENEWAL_LOOKBACK_DAYS, at most MAX_PER_RUN per run, newest first):
  1. looks up the customer on Paystack and lists that customer's SUCCESSFUL transactions since the current period started (all GET, nothing else);
  2. keeps a transaction only if it is not already one of our payments, carries the configured plan code, and was paid after the current period started;
  3. records each as a PENDING payment (never access) and calls `subscriptions.reconcile`, the SAME function the webhook uses: it re-reads the transaction from /transaction/verify, and applies it only if verify says
     success and price plus Paystack's allowed fee, currency, e-mail and plan all pass, AND Paystack's subscription record shows a next payment date later than our period end. Access is never extended from a date alone:
     with no successful transaction found, nothing is written and nothing is applied.
Idempotent with the webhook: the same payment_events key (`sub-charge:<reference>`), the same unique payment reference, so a payment the webhook and this job both see gives one row and one extension.

Nothing here places or changes a trade, touches a credential, or sends money. It never logs a key, a token, a signature or an e-mail address (customer ids are cut to 8 characters, references to their last 6)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..config import Settings
from ..db import session_scope
from ..models import BillingPayment, Customer, Subscription
from . import paystack_sub as ps
from . import subscriptions as subs
from .logsafe import safe_cause

logger = logging.getLogger(__name__)

RENEWAL_LOOKBACK_DAYS = 30  # only subscriptions whose period ended this recently are checked
MAX_PER_RUN = 25


def enabled(settings: Settings) -> bool:
    return bool(settings.subscription_billing_enabled and settings.paystack_renewal_reconcile and settings.paystack_secret_key and settings.paystack_plan_code)


def _due(db, now: datetime) -> list[tuple[str, str]]:
    rows = (db.query(Subscription).filter(Subscription.provider == subs.PROVIDER, Subscription.status == "active", Subscription.current_period_end.isnot(None),
                                          Subscription.current_period_end < now, Subscription.current_period_end > now - timedelta(days=RENEWAL_LOOKBACK_DAYS))
            .order_by(Subscription.current_period_end.desc()).limit(MAX_PER_RUN).all())
    return [(r.id, r.customer_id) for r in rows]


async def _check_one(settings: Settings, subscription_id: str, customer_id: str, now: datetime, counts: dict) -> None:
    with session_scope() as db:
        row = db.get(Subscription, subscription_id)
        customer = db.get(Customer, customer_id)
        if row is None or customer is None or row.provider_subscription_ref is None:
            counts["skipped"] += 1
            return
        email, period_start, old_end = customer.email, subs._aware(row.current_period_start), subs._aware(row.current_period_end)
        known = {b.reference for b in db.query(BillingPayment).filter_by(provider=subs.PROVIDER, customer_id=customer_id).all()}
        pending_already = any(b.status == "pending" for b in db.query(BillingPayment).filter_by(provider=subs.PROVIDER, customer_id=customer_id).all())
        # ---- reads only: no write lock is held while Paystack is asked
        cust = await ps.fetch_customer(email, settings)
        cust_id = ps.as_int(cust.get("id")) if cust else None
        if cust_id is None:
            subs.log_outcome(logging.INFO, "no_change", "renewal_job_customer_not_found", customer_id)
            counts["no_transaction"] += 1
            return
        candidates = await ps.list_customer_transactions(cust_id, period_start or (now - timedelta(days=RENEWAL_LOOKBACK_DAYS)), settings)
        fresh: list[str] = []
        for t in candidates:
            ref = ps.as_str(t.get("reference"))
            if not ref or ref in known or t.get("status") != "success":
                continue
            paid_at = ps.parse_dt(t.get("paid_at"))
            if period_start is not None and (paid_at is None or paid_at <= period_start):
                continue  # not a payment for THIS period
            verified = await ps.fetch_transaction(ref, settings)  # the list is only a hint; Paystack's own record decides
            if verified is None or verified.get("status") != "success" or ps.event_plan_code(verified) != settings.paystack_plan_code:
                continue  # not successful, or not a payment for our plan
            fresh.append(ref)
        if not fresh and not pending_already:
            subs.log_outcome(logging.INFO, "no_change", "renewal_job_no_successful_transaction", customer_id)
            counts["no_transaction"] += 1
            return
        # ---- write the pending rows, commit, then settle through the webhook's own function
        for ref in fresh:
            db.add(BillingPayment(customer_id=customer_id, provider=subs.PROVIDER, reference=ref[:100], amount_minor=subs._expected_base(db, customer_id, settings), currency=ps.PLAN_CURRENCY, status="pending"))
        try:
            db.commit()
        except Exception:  # noqa: BLE001 - a concurrent webhook recorded the same reference first: that is the idempotency working
            db.rollback()
        alerts = subs.Out()
        try:
            await subs.reconcile(db, customer, settings, now, alerts, hint_code=row.provider_subscription_ref)
        except (subs.RetryLater, ps.ProviderUnavailable) as exc:
            counts["not_ready"] += 1
            subs.log_outcome(logging.WARNING, "retry", f"renewal_job_{type(exc).__name__}", customer_id)
            await subs._send_alerts(settings, alerts, now)
            return
        db.commit()
        db.refresh(row)
        new_end = subs._aware(row.current_period_end)
        if new_end is not None and old_end is not None and new_end > old_end:
            counts["recovered"] += 1
            alerts.append(("renewal_recovered_by_job", customer_id))
            subs.log_outcome(logging.WARNING, "applied", "renewal_job_recovered_missed_renewal", customer_id)
        else:
            counts["no_change"] += 1
        await subs._send_alerts(settings, alerts, now)


async def run_once(settings: Settings, now: datetime | None = None) -> dict:
    """One pass. Never raises: a failure on one subscription is logged (class and a one-line cause) and the next one is tried."""
    counts = {"checked": 0, "recovered": 0, "no_transaction": 0, "not_ready": 0, "no_change": 0, "skipped": 0, "errors": 0}
    if not enabled(settings):
        return counts
    now = now or datetime.now(timezone.utc)
    with session_scope() as db:
        due = _due(db, now)
    for sub_id, cust_id in due:
        counts["checked"] += 1
        try:
            await _check_one(settings, sub_id, cust_id, now, counts)
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            logger.warning("billing: outcome=error reason=renewal_job_exception customer=%s cause=%s: %s", subs._mask(cust_id), type(exc).__name__, safe_cause(exc))
    logger.info("billing: renewal job checked=%(checked)d recovered=%(recovered)d no_transaction=%(no_transaction)d not_ready=%(not_ready)d no_change=%(no_change)d skipped=%(skipped)d errors=%(errors)d", counts)
    return counts


def main(argv: list[str] | None = None) -> int:
    """`python -m mba_bot.billing.renewal_job`: one pass by hand (needs the same two switches on). Prints the counts only."""
    import asyncio
    import sys

    from ..config import load_settings
    from ..db import init_db

    settings = load_settings()
    if not enabled(settings):
        print("renewal job: off (set SUBSCRIPTION_BILLING_ENABLED, PAYSTACK_RENEWAL_RECONCILE, PAYSTACK_SECRET_KEY and PAYSTACK_PLAN_CODE)", file=sys.stderr)
        return 2
    init_db(settings.database_url)
    counts = asyncio.run(run_once(settings))
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
