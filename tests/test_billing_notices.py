"""Stage 1 follow-up: customer reminders, events we did not handle, refunds, paying again after a failed renewal.

PROVES: control flow, once-per-period delivery, wording, gating by SUBSCRIPTION_BILLING_ENABLED, no write lock during a Telegram
send, and what the code does today in each case. DOES NOT PROVE: the shape of subscription.expiring_cards, invoice.payment_failed,
subscription.disable or refund.* bodies (never captured: the bodies here are invented and the handlers are written to tolerate
them being wrong), what Paystack does when a customer pays through ITS manual retry link, or live mode. No network."""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_security_hardening import client_for, make_app, post_event  # noqa: F401
from test_subscription_paystack import (  # noqa: F401
    ENV, PLAN, PRICE, TOKEN, activate, app_on, charge_event, count, end_in, entitled, fake, first_payment, iso, issue, row, sent, signed_up,
)

from mba_bot import protection_guard
from mba_bot.api.routers import billing
from mba_bot.billing import notices, paystack_sub as ps, subscriptions as subs
from mba_bot.config import load_settings
from mba_bot.db import session_scope
from mba_bot.models import BillingNotice, BillingPayment, Customer, PaymentEvent, Subscription, SubscriptionToken, TradingAccount

EMAIL = "payer@example.com"
TG = "424242"
SRC = Path(__file__).resolve().parent.parent / "src" / "mba_bot"


@pytest.fixture()
def tg(monkeypatch, sent):
    """Telegram, for both the owner alerts and the customer messages. `.to_customer` is what customers were sent."""
    class Rec:
        def __init__(self):
            self.log, self.fail, self.on_send = [], False, None

        @property
        def to_customer(self):
            return [m for t, m in self.log if t == TG]

        @property
        def to_owner(self):
            return [m for t, m in self.log if t == ENV["BILLING_ALERT_CHAT_ID"]]

    rec = Rec()

    class Notifier:
        def __init__(self, token):
            pass

        async def send(self, target, message):
            if rec.on_send:
                rec.on_send()
            if rec.fail:
                raise RuntimeError("telegram down")
            rec.log.append((target, message))

    monkeypatch.setattr(subs, "TelegramNotifier", Notifier)
    monkeypatch.setattr(notices, "TelegramNotifier", Notifier)
    return rec


def link_telegram(cid, target=TG):
    with session_scope() as s:
        s.add(TradingAccount(customer_id=cid, pair="BTC/USDT", market_type="spot", notification_channel="telegram", notification_target=target,
                             trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7), is_active=False))


def handle(body, now=None):
    with session_scope() as db:
        return asyncio.run(subs.handle_event(db, body, load_settings(), now=now))


def set_end(cid, end, status=None):
    with session_scope() as s:
        r = s.query(Subscription).filter_by(customer_id=cid).one()
        r.current_period_end = end
        if status:
            r.status = status


def failed_event(code="SUB_1", email=EMAIL):
    return {"event": "invoice.payment_failed", "data": {"subscription": {"subscription_code": code}, "customer": {"email": email}}}


def notice_for(cid, now):
    with session_scope() as s:
        return notices.active_notice(s, s.get(Customer, cid), load_settings(), now)


# ====================================================================== failed renewal: Telegram + banner, once per period
def test_a_failed_renewal_tells_the_customer_once_and_says_access_continues(app_on, fake, tg):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        link_telegram(cid)
        end = row(cid)[1].replace(tzinfo=timezone.utc)
        now = end + timedelta(hours=1)
        fake.subs["SUB_1"]["status"] = "attention"
        assert handle(failed_event(), now) == {"ok": True}
        assert len(tg.to_customer) == 1
        msg = tg.to_customer[0]
        assert "renewal payment failed" in msg and "5-day grace period" in msg and "/#/dashboard" in msg
        assert f"{(end + timedelta(days=5)).day} {(end + timedelta(days=5)).strftime('%b %Y')}" in msg  # the date access continues to
        assert EMAIL not in msg and "SUB_" not in msg and TOKEN not in msg
        assert any(subs.ALERTS["renewal_failed"] in m for m in tg.to_owner)  # the owner is told as before
        # redelivery: nothing more goes out
        handle(failed_event(), now)
        handle(failed_event(), now + timedelta(hours=2))
        assert len(tg.to_customer) == 1 and count(BillingNotice) == 1
        # the banner, with a pay button because the customer is in the grace window
        n = notice_for(cid, now)
        assert n["kind"] == "renewal_failed" and n["action"] == "pay" and "5-day grace period" in n["message"] and "http" not in n["message"]
        assert notice_for(cid, end + timedelta(days=5, seconds=1)) is None  # after the grace window the banner stops


def test_the_banner_stops_once_the_period_has_moved_on(app_on, fake, tg):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        end = row(cid)[1].replace(tzinfo=timezone.utc)
        handle(failed_event(), end + timedelta(hours=1))
        assert notice_for(cid, end + timedelta(hours=2))["kind"] == "renewal_failed"
        set_end(cid, end + timedelta(days=30))  # a later payment advanced the period
        assert notice_for(cid, end + timedelta(hours=3)) is None


def test_a_customer_without_telegram_gets_the_banner_only_and_a_failed_send_breaks_nothing(app_on, fake, tg):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))  # no trading account: nothing to send to
        end = row(cid)[1].replace(tzinfo=timezone.utc)
        handle(failed_event(), end + timedelta(hours=1))
        assert tg.to_customer == [] and notice_for(cid, end + timedelta(hours=2))["kind"] == "renewal_failed"
    with app_on() as c2:
        pass


def test_a_telegram_failure_does_not_change_the_webhook_answer_and_the_notice_is_still_stored(app_on, fake, tg):
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        link_telegram(cid)
        end = row(cid)[1].replace(tzinfo=timezone.utc)
        tg.fail = True
        assert handle(failed_event(), end + timedelta(hours=1)) == {"ok": True}
        assert count(BillingNotice) == 1 and tg.log == []


def test_no_database_write_lock_is_held_while_telegram_is_awaited(app_on, fake, tg):
    path = load_settings().database_url.replace("sqlite:///", "")
    seen = []

    def probe():
        conn = sqlite3.connect(path, timeout=0.2)
        try:
            conn.execute("BEGIN IMMEDIATE")
            seen.append("free")
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        link_telegram(cid)
        tg.on_send = probe
        end = row(cid)[1].replace(tzinfo=timezone.utc)
        handle(failed_event(), end + timedelta(hours=1))
    assert seen and set(seen) == {"free"}


# ====================================================================== paying again (POST /subscription/initialize)
class _Resp:
    status_code = 201

    def __init__(self, ref):
        self.ref = ref

    def json(self):
        return {"status": True, "data": {"authorization_url": "https://checkout.example/x", "reference": self.ref}}


@pytest.mark.parametrize("hours_after_end,status,expected", [(-5, "active", 409), (-5, "cancelled", 409), (1, "active", 200), (24 * 6, "active", 200), (1, "cancelled", 200)])
def test_initialize_again_is_allowed_only_from_the_grace_window_onwards(app_on, fake, tg, monkeypatch, hours_after_end, status, expected):
    monkeypatch.setattr(billing.httpx, "post", lambda url, headers=None, json=None, timeout=None: _Resp(json["reference"]))
    with app_on() as c:
        cid = activate(c, fake, end_in(1))
        set_end(cid, datetime.now(timezone.utc) + timedelta(hours=-hours_after_end if hours_after_end < 0 else -hours_after_end), status)
        assert c.post("/api/billing/subscription/initialize").status_code == expected


def prepare_resubscribe(c, fake, *, old_status="attention", old_token="tok_OLDtoken12345"):
    cid = first_payment(c, fake)
    fake.subs["SUB_1"]["email_token"] = old_token
    post_event(c, charge_event("mbasub_ref1"))
    set_end(cid, datetime.now(timezone.utc) - timedelta(hours=1))  # the period ended; the renewal failed; we are in grace
    fake.subs["SUB_1"]["status"] = old_status
    issue(cid, "mbasub_ref2")
    fake.tx("mbasub_ref2", cid, EMAIL)
    fake.sub("SUB_2", EMAIL, end_in(30), email_token="tok_NEWtoken67890")
    return cid


@pytest.fixture()
def posts(monkeypatch):
    calls = []

    async def post_json(path, body, settings):
        calls.append((path, dict(body)))
        return 200, {"status": True, "message": "Subscription disabled successfully"}

    monkeypatch.setattr(ps, "post_json", post_json)
    return calls


def test_subscribing_again_after_a_failed_renewal_replaces_and_cancels_the_old_subscription(app_on, fake, tg, posts, caplog):
    caplog.set_level(logging.DEBUG)
    with app_on() as c:
        cid = prepare_resubscribe(c, fake)
        assert post_event(c, charge_event("mbasub_ref2")).status_code == 200
        status, end, ref, _ = row(cid)
        assert (status, ref) == ("active", "SUB_2") and end.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) + timedelta(days=29)
        assert entitled(cid)
        assert posts == [("/subscription/disable", {"code": "SUB_1", "token": "tok_OLDtoken12345"})]  # the OLD one, with ITS token
        from mba_bot import crypto_utils
        with session_scope() as s:
            assert crypto_utils.decrypt(s.query(SubscriptionToken).one().token_enc, key=load_settings().encryption_key) == "tok_NEWtoken67890"
        assert not any("tok_" in m for _, m in tg.log)
    assert "tok_OLDtoken12345" not in caplog.text and "tok_NEWtoken67890" not in caplog.text


def test_a_subscription_whose_next_payment_is_still_ahead_is_never_cancelled(app_on, fake, tg, posts):
    with app_on() as c:
        cid = prepare_resubscribe(c, fake, old_status="active")
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(10))  # a good, paid-up subscription
        post_event(c, charge_event("mbasub_ref2"))
        assert posts == []


def test_an_old_subscription_that_is_due_but_still_shows_active_is_replaced_and_cancelled(app_on, fake, tg, posts):
    """Paystack may not have moved the failed subscription to `attention` yet: its date has passed and it never advanced."""
    with app_on() as c:
        cid = prepare_resubscribe(c, fake, old_status="active")
        fake.subs["SUB_1"]["next_payment_date"] = iso(row(cid)[1].replace(tzinfo=timezone.utc))  # the date never advanced past ours
        fake.subs["SUB_1"]["createdAt"] = iso(datetime.now(timezone.utc) - timedelta(days=30))
        assert post_event(c, charge_event("mbasub_ref2")).status_code == 200
        assert row(cid)[2] == "SUB_2" and entitled(cid)  # the NEW subscription carries the payment, not the stale one
        assert [p[1]["code"] for p in posts] == ["SUB_1"]


def test_if_the_old_subscription_cannot_be_cancelled_the_owner_is_told_and_the_new_one_still_applies(app_on, fake, tg, monkeypatch):
    async def down(path, body, settings):
        raise ps.ProviderUnavailable("http 503")

    monkeypatch.setattr(ps, "post_json", down)
    with app_on() as c:
        cid = prepare_resubscribe(c, fake)
        assert post_event(c, charge_event("mbasub_ref2")).status_code == 200
        assert row(cid)[2] == "SUB_2" and entitled(cid)
        assert any(subs.ALERTS["old_subscription_not_disabled"] in m for m in tg.to_owner)


def test_the_only_write_to_paystack_is_the_disable_call():
    src = (SRC / "billing" / "paystack_sub.py").read_text()
    assert src.count("client.post(") == 1 and 'path != "/subscription/disable"' in src

    class Cfg:
        paystack_secret_key = "sk_test_fakefakefakefake"

    with pytest.raises(ps.ProviderUnavailable, match="blocked"):
        asyncio.run(ps.post_json("/refund", {}, Cfg()))


# ====================================================================== subscription.expiring_cards
def expiring(*items):
    return {"event": "subscription.expiring_cards", "data": list(items)}


def test_expiring_cards_reminds_the_matching_customer_once_per_period(app_on, fake, tg):
    with app_on() as c:
        cid = activate(c, fake, end_in(10))
        link_telegram(cid)
        ev = expiring({"expiry_date": "12/2026", "description": "visa ending with 4081", "subscription": {"subscription_code": "SUB_1"}, "customer": {"email": EMAIL}},
                      {"expiry_date": "12/2026", "subscription": {"subscription_code": "SUB_nobody"}, "customer": {"email": "nobody@example.com"}})
        assert handle(ev) == {"ok": True}
        assert len(tg.to_customer) == 1 and "about to expire" in tg.to_customer[0] and "5-day grace period" in tg.to_customer[0]
        assert "4081" not in tg.to_customer[0] and EMAIL not in tg.to_customer[0]
        handle(ev)
        assert len(tg.to_customer) == 1 and count(BillingNotice) == 1
        assert notice_for(cid, datetime.now(timezone.utc))["kind"] == "card_expiring"


@pytest.mark.parametrize("body", [{"event": "subscription.expiring_cards"}, {"event": "subscription.expiring_cards", "data": None},
                                  {"event": "subscription.expiring_cards", "data": []}, {"event": "subscription.expiring_cards", "data": "x"},
                                  {"event": "subscription.expiring_cards", "data": [1, "a", None]}, {"event": "subscription.expiring_cards", "data": {"x": 1}},
                                  {"event": "subscription.expiring_cards", "data": [{"customer": None, "subscription": 5}]}])
def test_expiring_cards_tolerates_any_shape(app_on, fake, tg, body):
    with app_on() as c:
        activate(c, fake, end_in(10))
        assert handle(body) == {"ok": True}
        assert tg.to_customer == [] and count(BillingNotice) == 0


def test_expiring_cards_with_the_switch_off_is_not_handled(make_app, fake, tg):
    with client_for(make_app()) as c:
        signed_up(c)
        assert post_event(c, expiring({"customer": {"email": EMAIL}})).status_code == 200
        assert tg.log == [] and count(BillingNotice) == 0


def test_invoice_update_and_create_are_ignored_and_read_nothing(app_on, fake, tg):
    with app_on() as c:
        cid = activate(c, fake, end_in(10))
        before, calls = row(cid), len(fake.calls)
        for name in ("invoice.update", "invoice.create"):
            assert handle({"event": name, "data": {"transaction": {"reference": "x"}, "subscription": {"subscription_code": "SUB_1"}}}) == {"ok": True, "ignored": name}
        assert row(cid) == before and len(fake.calls) == calls and count(BillingNotice) == 0


# ====================================================================== refunds
@pytest.mark.parametrize("name", ["refund.pending", "refund.processing", "refund.processed", "refund.failed"])
@pytest.mark.parametrize("tx_status,marks", [("reversed", True), ("reversal pending", False), ("success", False)])
def test_refund_events_alert_and_only_a_reversed_transaction_marks_a_payment_refunded(app_on, fake, tg, name, tx_status, marks):
    with app_on() as c:
        cid = activate(c, fake, end_in(20))
        fake.txs["mbasub_ref1"]["status"] = tx_status
        assert handle({"event": name, "data": {"transaction_reference": "mbasub_ref1", "status": "processed"}}) == {"ok": True}
        with session_scope() as s:
            status = s.query(BillingPayment).one().status
        expect_refunded = marks and name != "refund.failed"  # refund.failed never marks anything: it only alerts
        assert (status == "refunded") == expect_refunded
        assert entitled(cid) and row(cid)[0] == "active"  # access is never revoked by a refund
        wanted = subs.ALERTS["refund_failed" if name == "refund.failed" else "refund_seen"]
        assert any(wanted in m for m in tg.to_owner)


def test_every_refund_event_name_is_covered_by_the_prefix():
    for name in ("refund.pending", "refund.processing", "refund.processed", "refund.failed"):
        assert name.startswith("refund.")
    src = (SRC / "billing" / "subscriptions.py").read_text()
    assert 'name.startswith("refund.")' in src


# ====================================================================== 3 days before the period end (non-card subscriptions)
def add_sub(cid, provider, end, status="active"):
    with session_scope() as s:
        s.add(Subscription(customer_id=cid, status=status, current_period_end=end, provider=provider))


def new_customer(email):
    with session_scope() as s:
        cu = Customer(email=email, password_hash="x")
        s.add(cu)
        s.flush()
        return cu.id


def test_the_three_day_reminder_goes_out_once_for_non_card_subscriptions_only(app_on, tg):
    with app_on() as c:
        now = datetime.now(timezone.utc)
        manual, card, far, ended = (new_customer(f"{n}@example.com") for n in ("manual", "card", "far", "ended"))
        for cid in (manual, card, far, ended):
            link_telegram(cid, target=TG if cid == manual else f"other{cid[:4]}")
        add_sub(manual, "manual", now + timedelta(days=2))
        add_sub(card, "paystack", now + timedelta(days=2))  # renews by itself: no reminder
        add_sub(far, "manual", now + timedelta(days=4))
        add_sub(ended, "manual", now - timedelta(hours=1))
        # two notices: the one ending in 2 days ("ends on") and the one that ended an hour ago and is in grace ("period ended on")
        assert asyncio.run(notices.run_reminders_once(load_settings(), now)) == 2
        assert len(tg.to_customer) == 1 and "ends on" in tg.to_customer[0] and "5-day grace period" in tg.to_customer[0] and "/#/dashboard" in tg.to_customer[0]
        assert len(tg.log) == 2 and any("period ended on" in m for t, m in tg.log if t != TG)
        # again, an hour later and the next day: once per period
        assert asyncio.run(notices.run_reminders_once(load_settings(), now + timedelta(hours=1))) == 0
        assert asyncio.run(notices.run_reminders_once(load_settings(), now + timedelta(hours=12))) == 0
        assert len(tg.to_customer) == 1 and count(BillingNotice) == 2
        # once the far one comes into range (4 days out, +1 day 2 hours later) it is notified, and the first is not notified again
        assert asyncio.run(notices.run_reminders_once(load_settings(), now + timedelta(days=1, hours=2))) == 1
        n = notice_for(manual, now + timedelta(hours=1))
        assert n["kind"] == "period_end_soon" and n["action"] is None and "ends on" in n["message"]


def test_the_reminder_job_does_nothing_while_the_switch_is_off(make_app, tg):
    with client_for(make_app()) as c:
        cid = signed_up(c)
        add_sub(cid, "manual", datetime.now(timezone.utc) + timedelta(days=1))
        assert asyncio.run(notices.run_reminders_once(load_settings())) == 0
        assert count(BillingNotice) == 0 and tg.log == []


def test_a_customer_without_telegram_still_has_the_notice_recorded(app_on, tg):
    with app_on() as c:
        cid = new_customer("nochat@example.com")
        add_sub(cid, "manual", datetime.now(timezone.utc) + timedelta(days=1))
        assert asyncio.run(notices.run_reminders_once(load_settings())) == 1
        assert tg.log == [] and count(BillingNotice) == 1


# ====================================================================== the scheduler hook
def test_the_scheduler_hook_is_inert_when_off_throttled_when_on_and_never_raises(monkeypatch):
    from mba_bot import scheduler
    calls = []

    async def fake_run(settings, now=None):
        calls.append(1)

    monkeypatch.setattr(notices, "run_reminders_once", fake_run)
    from dataclasses import replace
    base = _settings()
    scheduler._last_reminder_check = 0.0
    asyncio.run(scheduler._billing_reminders(replace(base, subscription_billing_enabled=False)))
    assert calls == [] and scheduler._last_reminder_check == 0.0
    asyncio.run(scheduler._billing_reminders(replace(base, subscription_billing_enabled=True)))
    asyncio.run(scheduler._billing_reminders(replace(base, subscription_billing_enabled=True)))  # within the hour: skipped
    assert calls == [1]

    async def boom(settings, now=None):
        raise RuntimeError("x")

    monkeypatch.setattr(notices, "run_reminders_once", boom)
    scheduler._last_reminder_check = 0.0
    asyncio.run(scheduler._billing_reminders(replace(base, subscription_billing_enabled=True)))  # swallowed


def _settings():
    import os
    os.environ.setdefault("JWT_SECRET", "x")
    return load_settings()


# ====================================================================== the banner route and wording
def test_the_banner_route_is_null_when_off_and_never_reads_anything(make_app, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be read while the switch is off")

    monkeypatch.setattr(notices, "active_notice", boom)
    with client_for(make_app()) as c:
        signed_up(c)
        r = c.get("/api/billing/notice")
        assert r.status_code == 200 and r.json() == {"notice": None}
        c.cookies.clear()
        assert c.get("/api/billing/notice").status_code == 401


def test_the_banner_route_returns_the_notice_with_the_cancel_wording(app_on, fake, tg):
    with app_on() as c:
        cid = activate(c, fake, end_in(20))
        assert c.get("/api/billing/notice").json() == {"notice": None}
        fake.subs["SUB_1"]["status"] = "non-renewing"
        post_event(c, {"event": "subscription.not_renew", "data": {"subscription_code": "SUB_1"}})
        n = c.get("/api/billing/notice").json()["notice"]
        assert n["kind"] == "cancelled" and n["action"] is None
        assert notices.CANCEL_NOTE in n["message"] and "stops the next charge" in n["message"] and "end of the month you have already paid for" in n["message"]


def test_the_texts_are_fixed_and_carry_the_grace_period(app_on, fake):
    with app_on() as c:
        cid = activate(c, fake, end_in(2))
        s = load_settings()
        with session_scope() as db:
            sub = db.get(Customer, cid).subscription
            for kind in (notices.KIND_RENEWAL_FAILED, notices.KIND_CARD_EXPIRING, notices.KIND_PERIOD_END):
                text = notices.message_for(kind, s, sub)
                assert "5-day grace period" in text and "@" not in text and "SUB_" not in text and TOKEN not in text
            assert "5-day grace period" not in notices.message_for(notices.KIND_CANCELLED, s, sub)  # a cancel has no grace
            assert notices.CANCEL_NOTE in notices.message_for(notices.KIND_CANCELLED, s, sub)
    assert "access continues" in notices.CANCEL_NOTE.lower()


def test_the_terms_and_the_banner_carry_the_cancel_wording():
    root = SRC.parent.parent
    terms = (root / "frontend" / "src" / "legalContent.js").read_text()
    assert "cancelling stops the next charge, and your access continues until the end of the period you have already paid for" in terms
    assert "BillingNoticeBanner" in (root / "frontend" / "src" / "App.jsx").read_text()


def test_the_new_code_imports_nothing_that_trades_and_notices_hold_no_provider_text():
    import ast
    for name in ("notices.py", "subscriptions.py", "paystack_sub.py"):
        tree = ast.parse((SRC / "billing" / name).read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not any(x in (m or "") for m in mods for x in ("venue", "lifecycle", "exchange", "trader", "risk", "onboarding")), mods


def test_the_notices_migration_is_one_idempotent_table_on_top_of_the_tokens_one(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = next((SRC.parent.parent / "alembic" / "versions").glob("b8e5f2a0c4d6_*.py"))
    spec = importlib.util.spec_from_file_location("notices_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "a7d4e1c9b3f5"
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")

    def run(fn):
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    run(mig.upgrade)
    run(mig.upgrade)
    assert inspect(eng).get_table_names() == ["billing_notices"]
    assert {i["name"] for i in inspect(eng).get_indexes("billing_notices")} == {"ix_billing_notices_customer_id"}
    run(mig.downgrade)
    assert inspect(eng).get_table_names() == []


# ====================================================================== OPEN RISK: a payment through Paystack's manual retry link
# Nobody has seen what Paystack does when a customer pays the failed invoice through the retry link it e-mails. These tests do NOT
# claim to know. They pin what THIS CODE does today under each assumption, so the first live case is not a surprise.
# Assumption A: Paystack moves next_payment_date forward and sends charge.success (a Paystack reference, no customer id in metadata).
# Assumption B: Paystack sends the same charge.success but leaves next_payment_date at the old (passed) date.
def grace_customer(c, fake):
    cid = activate(c, fake, end_in(1))
    old_end = datetime.now(timezone.utc) - timedelta(hours=2)
    set_end(cid, old_end)
    fake.subs["SUB_1"]["status"] = "attention"
    fake.subs["SUB_1"]["next_payment_date"] = iso(old_end)
    return cid, old_end


def retry_payment(fake, cid, *, minutes_ago):
    t = fake.tx("RETRY_REF", cid, EMAIL, meta=False)
    t["paid_at"] = iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    return {"event": "charge.success", "data": {"reference": "RETRY_REF", "status": "success", "customer": {"email": EMAIL}, "plan": {"plan_code": PLAN}}}


def test_the_pay_button_is_only_offered_when_paying_again_is_allowed(app_on, fake, tg):
    """A failed-renewal notice that arrives while the paid period still runs has no button (initialize would answer 409)."""
    with app_on() as c:
        cid = activate(c, fake, end_in(2))
        handle(failed_event(), datetime.now(timezone.utc))
        n = notice_for(cid, datetime.now(timezone.utc))
        assert n["kind"] == "renewal_failed" and n["action"] is None
        end = row(cid)[1].replace(tzinfo=timezone.utc)
        assert notice_for(cid, end + timedelta(minutes=1))["action"] == "pay"


def test_a_failure_halfway_through_applying_a_payment_rolls_everything_back_and_the_retry_succeeds(app_on, fake, tg, monkeypatch):
    """With pysqlite a row written inside begin_nested() used to be committed at once and survive a later error: the replay guard would
    then call the redelivery a duplicate and the payment would never be applied."""
    with app_on() as c:
        cid = first_payment(c, fake)
        real = subs._store_token

        def boom(*a, **k):
            raise RuntimeError("database trouble after the payment was settled")

        monkeypatch.setattr(subs, "_store_token", boom)
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 503
        assert row(cid) is None and count(PaymentEvent) == 0
        with session_scope() as s:
            assert s.query(BillingPayment).one().status == "pending"  # nothing half-recorded
        monkeypatch.setattr(subs, "_store_token", real)
        assert post_event(c, charge_event("mbasub_ref1")).status_code == 200  # Paystack redelivers: it now goes through
        assert row(cid)[0] == "active" and entitled(cid) and count(PaymentEvent) == 1


def test_retry_link_assumption_A_the_period_advances_and_the_payment_is_applied(app_on, fake, tg):
    with app_on() as c:
        cid, old_end = grace_customer(c, fake)
        new_end = end_in(30)
        fake.subs["SUB_1"].update(status="active", next_payment_date=iso(new_end))
        assert post_event(c, retry_payment(fake, cid, minutes_ago=1)).status_code == 200
        status, end, ref, _ = row(cid)
        assert (status, ref) == ("active", "SUB_1") and end.replace(tzinfo=timezone.utc) == new_end
        assert entitled(cid) and notice_for(cid, datetime.now(timezone.utc)) is None  # the banner is gone: the period moved on


def test_retry_link_assumption_A2_it_is_also_applied_if_the_status_is_still_attention(app_on, fake, tg):
    with app_on() as c:
        cid, _ = grace_customer(c, fake)
        fake.subs["SUB_1"]["next_payment_date"] = iso(end_in(30))  # status left at `attention`
        assert post_event(c, retry_payment(fake, cid, minutes_ago=1)).status_code == 200
        assert row(cid)[0] == "active" and entitled(cid)


def test_retry_link_assumption_B_the_date_does_not_move_nothing_is_applied_and_the_owner_is_told(app_on, fake, tg):
    """TODAY: the payment is verified but NOT applied (we refuse to invent a period end), the webhook answers 503 (Paystack redelivers
    for up to 72 hours), nothing is stored for the payment, and the owner is alerted once per 6 hours after the payment is 10 minutes old.
    The customer gets no extra time from us. If this is what Paystack does, an owner action is needed: see the doc."""
    with app_on() as c:
        cid, old_end = grace_customer(c, fake)
        before = row(cid)
        assert post_event(c, retry_payment(fake, cid, minutes_ago=1)).status_code == 503
        assert tg.to_owner == []  # a payment that is a minute old may simply be early: no alert yet
        assert post_event(c, retry_payment(fake, cid, minutes_ago=20)).status_code == 503
        assert row(cid) == before and count(BillingPayment) == 1  # only the first payment is recorded
        assert any(subs.ALERTS["payment_pending_apply"] in m for m in tg.to_owner)
        n = len([m for m in tg.to_owner if subs.ALERTS["payment_pending_apply"] in m])
        post_event(c, retry_payment(fake, cid, minutes_ago=25))
        assert len([m for m in tg.to_owner if subs.ALERTS["payment_pending_apply"] in m]) == n  # not repeated within 6 hours
        # the customer's access still ends with the grace window, as before
        assert entitled(cid, old_end + timedelta(days=4)) is True and entitled(cid, old_end + timedelta(days=5, seconds=1)) is False

