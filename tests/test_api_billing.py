from datetime import datetime, timedelta, timezone

import pytest

from mba_bot.api.routers.billing import apply_subscription_event
from mba_bot.db import init_db, session_scope
from mba_bot.models import Customer, TradingAccount


@pytest.fixture()
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    with session_scope() as session:
        customer = Customer(email="a@example.com", password_hash="x", stripe_customer_id="cus_123")
        session.add(customer)
        session.flush()
        account = TradingAccount(
            customer_id=customer.id,
            pair="BTC/USDT",
            market_type="spot",
            notification_channel="telegram",
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
            is_active=False,
        )
        session.add(account)
    yield


def test_checkout_completed_activates_account(db):
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_123"}},
    }
    with session_scope() as session:
        customer = session.query(Customer).filter_by(stripe_customer_id="cus_123").first()
        apply_subscription_event(session, event)
        assert customer.subscription_status == "active"
        assert customer.account.is_active is True


def test_subscription_canceled_deactivates_account(db):
    with session_scope() as session:
        customer = session.query(Customer).filter_by(stripe_customer_id="cus_123").first()
        customer.account.is_active = True

    event = {
        "type": "customer.subscription.deleted",
        "data": {"object": {"customer": "cus_123", "status": "canceled"}},
    }
    with session_scope() as session:
        customer = session.query(Customer).filter_by(stripe_customer_id="cus_123").first()
        apply_subscription_event(session, event)
        assert customer.subscription_status == "canceled"
        assert customer.account.is_active is False


def test_past_due_deactivates_account(db):
    event = {
        "type": "customer.subscription.updated",
        "data": {"object": {"customer": "cus_123", "status": "past_due"}},
    }
    with session_scope() as session:
        apply_subscription_event(session, event)
        customer = session.query(Customer).filter_by(stripe_customer_id="cus_123").first()
        assert customer.subscription_status == "past_due"
        assert customer.account.is_active is False


def test_unknown_customer_is_a_noop(db):
    event = {"type": "checkout.session.completed", "data": {"object": {"customer": "cus_does_not_exist"}}}
    # Should not raise even with no matching customer in the DB.
    with session_scope() as session:
        apply_subscription_event(session, event)


def test_irrelevant_event_type_is_ignored(db):
    event = {"type": "invoice.paid", "data": {"object": {"customer": "cus_123"}}}
    with session_scope() as session:
        customer = session.query(Customer).filter_by(stripe_customer_id="cus_123").first()
        before = customer.subscription_status
        apply_subscription_event(session, event)
        assert customer.subscription_status == before
