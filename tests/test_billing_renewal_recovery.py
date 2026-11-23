"""Paystack renewals: a charge.success that Paystack's own record does not confirm yet is retried (503) and applied on redelivery, every silent or generic exit writes one greppable line with a reason code, a
renewal Paystack reports is recorded as PENDING at once (it never grants access), and the hourly recovery job (switch OFF by default) applies a missed renewal ONLY from a verified successful transaction.

PROVES: control flow against a FAKE Paystack (the one seam, billing.paystack_sub.get_json). DOES NOT PROVE: Paystack's real timing between charge.success and its records, its real list-transactions answer
(the shape is read tolerantly and UNVERIFIED), or anything about live mode. No network, no real key."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from test_security_hardening import make_app  # noqa: F401
from test_subscription_paystack import (ENV, FEES, PLAN, PRICE, FakePaystack, activate, app_on, charge_event, count, end_in, entitled, iso, post_event, renewal_event, row, sent, signed_up)  # noqa: F401

from mba_bot import scheduler
from mba_bot.api.routers import billing
from mba_bot.billing import paystack_sub as ps
from mba_bot.billing import renewal_job
from mba_bot.billing import subscriptions as subs
from mba_bot.billing.logsafe import safe_cause
from mba_bot.config import load_settings
from mba_bot.db import session_scope
from mba_bot.models import BillingPayment, PaymentEvent, Subscription

SECRETS = ("sk_" + "live_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",  # built in two pieces so no secret scanner mistakes this planted fake for a real key
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "tok_SECRETsubscriptiontoken123", "payer@example.com")
U = timezone.utc


class JobPaystack(FakePaystack):
    """FakePaystack plus the one extra read the job makes: the customer's successful transactions."""

    async def get_json(self, path, settings):
        if path.startswith("/transaction?"):
            self.calls.append(path)
            q = parse_qs(urlparse(path).query)
            assert q["status"] == ["success"] and "from" in q and q["customer"] == ["501"]
            return {"status": True, "data": [t for t in self.txs.values() if t.get("status") == "success" and str(t["customer"]["id"]) == q["customer"][0]]}
        return await super().get_json(path, settings)


@pytest.fixture()
def fake(monkeypatch):
    f = JobPaystack()
    monkeypatch.setattr(ps, "get_json", f.get_json)

    async def no_writes(*a, **k):
        raise AssertionError("the recovery job and the webhook must never write to Paystack")

    monkeypatch.setattr(ps, "post_json", no_writes)
    return f


def statuses():
    with session_scope() as s:
        return sorted(b.status for b in s.query(BillingPayment).all())


def lapse(cid, *, ended_hours_ago=2, started_days_ago=28):
    """Make the subscription's paid period end in the past (a renewal is due), as it would be an hour after a missed webhook."""
    with session_scope() as s:
        r = s.query(Subscription).filter_by(customer_id=cid).one()
        r.current_period_end = datetime.now(U) - timedelta(hours=ended_hours_ago)
        r.current_period_start = datetime.now(U) - timedelta(days=started_days_ago)
    return datetime.now(U) - timedelta(hours=ended_hours_ago)


def job(**over):
    return asyncio.run(renewal_job.run_once(load_settings()))


# ================================================================================ 1. verify not yet success: 503, then applied
@pytest.mark.parametrize("how", ["pending", "missing"])
def test_a_renewal_whose_verify_is_not_yet_success_gets_503_and_is_applied_on_redelivery(app_on, fake, how):
    old = end_in(1)
    with app_on() as c:
        cid = activate(c, fake, old)
        fake.tx("RENEW1", cid, "payer@example.com", meta=False, status="pending")
        if how == "missing":
            del fake.txs["RENEW1"]  # Paystack's verify answers "not found" for it yet
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30))
        assert post_event(c, renewal_event()).status_code == 503
        assert row(cid)[1].replace(tzinfo=U) == old and statuses() == ["paid", "pending"]  # recorded as pending, nothing extended
        assert entitled(cid, old + timedelta(hours=1)) is True and not entitled(cid, old + timedelta(days=5, seconds=1))  # the pending row grants nothing: only the paid period counts
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)  # Paystack's record caught up
        assert post_event(c, renewal_event()).status_code == 200
        assert row(cid)[1].replace(tzinfo=U) > old and statuses() == ["paid", "paid"] and count(PaymentEvent) == 2


def test_the_ten_minute_alert_covers_the_unverified_path(app_on, fake, sent):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        fake.tx("RENEW1", cid, "payer@example.com", meta=False, status="pending")
        assert post_event(c, renewal_event()).status_code == 503
        assert not [m for _, m in sent if subs.ALERTS["payment_pending_apply"] in m]  # a payment seconds old may just be early
        with session_scope() as s:
            s.query(BillingPayment).filter_by(reference="RENEW1").one().issued_at = datetime.now(U) - timedelta(minutes=20)
        assert post_event(c, renewal_event()).status_code == 503
        assert [m for _, m in sent if subs.ALERTS["payment_pending_apply"] in m]


def test_a_renewal_whose_date_has_not_moved_gets_503_then_applies(app_on, fake):
    old = end_in(1)
    with app_on() as c:
        cid = activate(c, fake, old)
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)
        assert post_event(c, renewal_event()).status_code == 503  # Paystack's subscription date is still the old one
        assert row(cid)[1].replace(tzinfo=U) == old and statuses() == ["paid", "pending"]
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30))
        assert post_event(c, renewal_event()).status_code == 200
        assert row(cid)[1].replace(tzinfo=U) > old and statuses() == ["paid", "paid"]


def test_a_pending_row_never_grants_access(app_on, fake):
    with app_on() as c:
        cid = signed_up(c)
        with session_scope() as s:
            s.add(BillingPayment(customer_id=cid, provider="paystack", reference="PENDING1", amount_minor=PRICE, currency="NGN", status="pending"))
        assert not entitled(cid) and row(cid) is None


# ================================================================================ 2. one greppable line per exit
def reasons(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("billing: outcome=")]


def test_the_verify_miss_logs_its_reason(app_on, fake, caplog):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        fake.tx("RENEW1", cid, "payer@example.com", meta=False, status="pending")
        with caplog.at_level(logging.INFO):
            assert post_event(c, renewal_event()).status_code == 503
        assert any("outcome=retry reason=verify_not_success" in m and "ref=...RENEW1"[-9:] in m for m in reasons(caplog))
        assert any("subscription event not settled yet (RetryLater: payment not verified yet)" in r.getMessage() for r in caplog.records)


def test_the_duplicate_exits_log_their_reasons(app_on, fake, caplog):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30))
        assert post_event(c, renewal_event()).status_code == 200
        with caplog.at_level(logging.INFO):
            assert post_event(c, renewal_event()).json().get("duplicate") is True  # the same event again
        assert any("outcome=duplicate reason=payment_already_paid" in m for m in reasons(caplog))
        # the event key already recorded by a concurrent delivery
        caplog.clear()
        issue_ref = "mbasub_dupkey"
        with session_scope() as s:
            s.add(BillingPayment(customer_id=cid, provider="paystack", reference=issue_ref, amount_minor=PRICE, currency="NGN", status="pending"))
            s.add(PaymentEvent(provider="paystack", event_key=f"sub-charge:{issue_ref}", reference=issue_ref, customer_id=cid, amount=PRICE, currency="NGN", outcome="applied"))
        fake.tx(issue_ref, cid, "payer@example.com")
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(40))
        with caplog.at_level(logging.INFO):
            assert post_event(c, charge_event(issue_ref)).status_code == 200
        assert any("outcome=duplicate reason=event_key_already_recorded" in m for m in reasons(caplog))


def test_the_missing_reference_the_unclassified_charge_and_the_ignored_event_log_their_reasons(app_on, fake, caplog):
    with app_on() as c:
        activate(c, fake, end_in(1))
        with caplog.at_level(logging.INFO):
            assert post_event(c, {"event": "charge.success", "data": {"status": "success", "plan": {"plan_code": PLAN}, "customer": {"email": "payer@example.com"}}}).status_code == 200
            assert post_event(c, {"event": "charge.success", "data": {"reference": "STRANGER1", "status": "success", "customer": {"email": "stranger@example.com"}}}).status_code == 200
            assert post_event(c, {"event": "invoice.create", "data": {}}).status_code == 200
            assert post_event(c, renewal_event("RENEWX", "stranger@example.com")).status_code == 200  # our plan, but no customer by that e-mail
        got = "\n".join(reasons(caplog))
        assert "outcome=ignored reason=missing_reference" in got
        assert "outcome=passed_on reason=charge_not_a_subscription_charge" in got
        assert "outcome=ignored reason=event_invoice_create" in got
        assert "outcome=ignored reason=unknown_customer" in got


def test_a_bad_signature_logs_its_reason_and_not_the_signature(app_on, fake, caplog):
    with app_on() as c:
        activate(c, fake, end_in(1))
        with caplog.at_level(logging.INFO):
            assert post_event(c, renewal_event(), signature="f" * 128).status_code == 400
        assert "paystack webhook rejected: reason=bad_signature" in caplog.text and "f" * 40 not in caplog.text


def test_a_generic_handler_failure_logs_the_class_and_a_one_line_cause_and_no_secret(app_on, fake, caplog, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError(f"explode Authorization: Bearer {SECRETS[0]}\nsecond line {SECRETS[1]} token={SECRETS[2]} owner {SECRETS[3]}")

    with app_on() as c:
        activate(c, fake, end_in(1))
        monkeypatch.setattr(subs, "_on_charge", boom)  # after the first payment: only the renewal event hits the failing handler
        with caplog.at_level(logging.INFO):
            assert post_event(c, renewal_event()).status_code == 503
        line = next(r.getMessage() for r in caplog.records if "subscription event handler failed" in r.getMessage())
        assert "reason=handler_exception" in line and "RuntimeError" in line and "explode" in line
        assert "\n" not in line and all(secret not in caplog.text for secret in SECRETS)


def test_safe_cause_keeps_the_class_message_start_and_removes_secrets():
    text = safe_cause(ValueError(f"bad apiKey={SECRETS[0]} for {SECRETS[3]} sig {SECRETS[1]}\nmore"))
    assert text.startswith("bad ") and not any(s in text for s in SECRETS) and "\n" not in text and len(text) <= 160
    assert safe_cause(RuntimeError("")) == "" and len(safe_cause(RuntimeError("x " * 500))) == 160


# ================================================================================ 3. pending rows
def test_a_renewal_is_visible_in_billing_payments_before_it_is_applied_and_a_redelivery_adds_no_second_row(app_on, fake):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)
        assert post_event(c, renewal_event()).status_code == 503
        assert post_event(c, renewal_event()).status_code == 503
        assert post_event(c, renewal_event()).status_code == 503
        with session_scope() as s:
            rows = s.query(BillingPayment).filter_by(reference="RENEW1").all()
            assert len(rows) == 1 and rows[0].status == "pending" and rows[0].amount_minor == PRICE and rows[0].customer_id == cid


# ================================================================================ 4. the hourly recovery job
def test_the_job_is_off_by_default_and_needs_both_switches(app_on, fake, monkeypatch):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        lapse(cid)
        assert load_settings().paystack_renewal_reconcile is False
        calls = len(fake.calls)
        assert job()["checked"] == 0 and len(fake.calls) == calls  # switch off: no Paystack call at all
    with app_on(PAYSTACK_RENEWAL_RECONCILE="1") as c:
        assert load_settings().paystack_renewal_reconcile is True and renewal_job.enabled(load_settings())
        monkeypatch.setenv("SUBSCRIPTION_BILLING_ENABLED", "0")
        assert not renewal_job.enabled(load_settings())  # the master switch off also stops it


def test_the_job_applies_a_renewal_that_had_no_webhook(app_on, fake, sent):
    with app_on(PAYSTACK_RENEWAL_RECONCILE="1") as c:
        cid = activate(c, fake, end_in(1))
        ended = lapse(cid)
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)  # Paystack charged it; our webhook never settled
        new = end_in(30, hours=5)
        fake.subs["SUB_1"]["next_payment_date"] = iso(new)
        assert statuses() == ["paid"] and not entitled(cid, datetime.now(U) + timedelta(days=6))  # before the job: lapsed
        counts = job()
        assert counts["recovered"] == 1 and counts["errors"] == 0
        assert row(cid)[1].replace(tzinfo=U) > ended and statuses() == ["paid", "paid"] and count(PaymentEvent) == 2
        assert entitled(cid)
        assert any(subs.ALERTS["renewal_recovered_by_job"] in m for _, m in sent)


def test_the_job_applies_nothing_when_no_successful_transaction_exists_even_if_paystacks_date_moved(app_on, fake):
    with app_on(PAYSTACK_RENEWAL_RECONCILE="1") as c:
        cid = activate(c, fake, end_in(1))
        lapse(cid)
        before = row(cid)
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30))  # a future date alone must never extend access
        fake.customers["payer@example.com"] = {"id": 501, "email": "payer@example.com"}
        counts = job()
        assert counts["recovered"] == 0 and counts["no_transaction"] == 1
        assert row(cid) == before and statuses() == ["paid"] and not entitled(cid, datetime.now(U) + timedelta(days=6))


@pytest.mark.parametrize("what", ["failed_status", "other_plan", "no_plan", "before_period", "other_email", "wrong_amount", "wrong_currency"])
def test_the_job_applies_nothing_for_a_transaction_that_fails_a_check(app_on, fake, what):
    with app_on(PAYSTACK_RENEWAL_RECONCILE="1") as c:
        cid = activate(c, fake, end_in(1))
        lapse(cid)
        before = row(cid)
        over = {"failed_status": dict(status="failed"), "other_plan": dict(plan="PLN_other"), "no_plan": dict(plan=None),
                "before_period": dict(paid_at=iso(datetime.now(U) - timedelta(days=60))), "other_email": dict(customer={"id": 501, "email": "other@example.com"}),
                "wrong_amount": dict(amount=PRICE + 999, requested=PRICE, fees=FEES), "wrong_currency": dict(currency="USD")}[what]
        fake.tx("RENEW1", cid, "payer@example.com", meta=False, **over)
        fake.customers["payer@example.com"] = {"id": 501, "email": "payer@example.com"}
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30))
        job()
        assert row(cid) == before and not entitled(cid, datetime.now(U) + timedelta(days=6)), what
        assert statuses().count("paid") == 1, what  # no second paid payment (a failed check may leave a failed or underpaid row, never a paid one)


def test_the_webhook_then_the_job_give_one_row_and_one_extension(app_on, fake):
    with app_on(PAYSTACK_RENEWAL_RECONCILE="1") as c:
        cid = activate(c, fake, end_in(1))
        lapse(cid)
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30, hours=5))
        assert post_event(c, renewal_event()).status_code == 200
        after_webhook = row(cid)
        counts = job()
        assert counts["recovered"] == 0 and row(cid)[1] == after_webhook[1] and statuses() == ["paid", "paid"] and count(PaymentEvent) == 2


def test_the_job_then_the_webhook_give_one_row_and_one_extension(app_on, fake):
    with app_on(PAYSTACK_RENEWAL_RECONCILE="1") as c:
        cid = activate(c, fake, end_in(1))
        lapse(cid)
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30, hours=5))
        assert job()["recovered"] == 1
        after_job = row(cid)
        answer = post_event(c, renewal_event())  # Paystack's webhook finally arrives
        assert answer.status_code == 200 and answer.json().get("duplicate") is True
        assert row(cid)[1] == after_job[1] and statuses() == ["paid", "paid"] and count(PaymentEvent) == 2
        assert job()["recovered"] == 0  # and a second pass changes nothing (the period has not ended again)


def test_the_job_is_read_only_logs_no_secret_and_skips_old_or_non_paystack_subscriptions(app_on, fake, caplog):
    with app_on(PAYSTACK_RENEWAL_RECONCILE="1") as c:
        cid = activate(c, fake, end_in(1))
        lapse(cid, ended_hours_ago=24 * 40)  # ended 40 days ago: outside the look-back
        fake.tx("RENEW1", cid, "payer@example.com", meta=False)
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30))
        calls = len(fake.calls)
        assert job()["checked"] == 0 and len(fake.calls) == calls
        lapse(cid, ended_hours_ago=3)
        with caplog.at_level(logging.INFO):
            counts = job()
        assert counts["recovered"] == 1
        assert all(c_.startswith(("/transaction", "/subscription", "/plan", "/customer")) for c_ in fake.calls)  # only GET paths; post_json would have raised
        assert all(secret not in caplog.text for secret in SECRETS)
        assert "billing: renewal job checked=1 recovered=1" in caplog.text


def test_a_failure_on_one_subscription_does_not_stop_the_job_and_logs_class_and_cause(app_on, fake, caplog, monkeypatch):
    with app_on(PAYSTACK_RENEWAL_RECONCILE="1") as c:
        cid = activate(c, fake, end_in(1))
        lapse(cid)

        async def broken(*a, **k):
            raise RuntimeError(f"kaboom {SECRETS[0]}")

        monkeypatch.setattr(ps, "fetch_customer", broken)
        with caplog.at_level(logging.INFO):
            counts = job()
        assert counts["errors"] == 1 and "renewal_job_exception" in caplog.text and "RuntimeError" in caplog.text and SECRETS[0] not in caplog.text


def test_the_scheduler_hook_does_nothing_with_the_switch_off_runs_hourly_and_never_raises(monkeypatch):
    ran = []

    async def fake_run(settings):
        ran.append(1)
        raise RuntimeError("must not escape")

    monkeypatch.setattr(renewal_job, "run_once", fake_run)
    monkeypatch.setattr(scheduler, "_last_renewal_job", 0.0)
    off = load_settings()
    asyncio.run(scheduler._renewal_reconcile(off))
    assert ran == []  # switch off
    import dataclasses

    on = dataclasses.replace(off, subscription_billing_enabled=True, paystack_renewal_reconcile=True)
    asyncio.run(scheduler._renewal_reconcile(on))
    asyncio.run(scheduler._renewal_reconcile(on))
    assert ran == [1]  # once; the second call within the hour is skipped; the exception was swallowed


# ================================================================================ 5. renewal wording
def test_the_options_for_a_lapsed_customer_carry_the_dates_the_renew_screen_shows(app_on, fake):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        ended = lapse(cid, ended_hours_ago=24 * 9)  # period ended 9 days ago: the 5-day grace has passed too
        o = c.get("/api/billing/options").json()
        assert o["state"] == "expired" and o["period_end"] is not None and o["grace_end"] is not None and o["grace_days"] == 5
        assert datetime.fromisoformat(o["period_end"].replace("Z", "+00:00")).replace(tzinfo=U if "+" not in o["period_end"] else None).date() == ended.date()
        late = lapse(cid, ended_hours_ago=24 * 2)
        assert c.get("/api/billing/options").json()["state"] == "grace"  # past due, inside the grace window: the dashboard card says Renew overdue
        assert late
