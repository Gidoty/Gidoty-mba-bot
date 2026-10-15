from datetime import datetime, timedelta, timezone

import pytest

from mba_bot.api.routers.billing import apply_payment_event
from mba_bot.db import init_db, session_scope
from mba_bot.models import Customer, TradingAccount


@pytest.fixture()
def customer_id(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    with session_scope() as session:
        customer = Customer(email="a@example.com", password_hash="x")
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
        session.flush()
        cid = customer.id
    yield cid


def test_charge_success_marks_paid_and_activates_account(customer_id):
    event = {
        "event": "charge.success",
        "data": {"status": "success", "reference": "ref_abc123", "metadata": {"customer_id": customer_id}},
    }
    with session_scope() as session:
        apply_payment_event(session, event)
        customer = session.get(Customer, customer_id)
        assert customer.has_paid is True
        assert customer.paystack_reference == "ref_abc123"
        assert customer.account.is_active is True


def test_has_paid_is_never_cleared_by_a_later_event(customer_id):
    with session_scope() as session:
        apply_payment_event(
            session,
            {"event": "charge.success", "data": {"status": "success", "reference": "r1", "metadata": {"customer_id": customer_id}}},
        )

    with session_scope() as session:
        # Lifetime access: nothing in the webhook flow should ever flip has_paid back off.
        apply_payment_event(session, {"event": "charge.failed", "data": {"status": "failed", "metadata": {"customer_id": customer_id}}})
        customer = session.get(Customer, customer_id)
        assert customer.has_paid is True


def test_non_success_status_is_ignored(customer_id):
    event = {"event": "charge.success", "data": {"status": "abandoned", "metadata": {"customer_id": customer_id}}}
    with session_scope() as session:
        apply_payment_event(session, event)
        customer = session.get(Customer, customer_id)
        assert customer.has_paid is False
        assert customer.account.is_active is False


def test_unknown_customer_is_a_noop(customer_id):
    event = {"event": "charge.success", "data": {"status": "success", "metadata": {"customer_id": "does-not-exist"}}}
    # Should not raise even with no matching customer in the DB.
    with session_scope() as session:
        apply_payment_event(session, event)


def test_missing_metadata_is_a_noop(customer_id):
    event = {"event": "charge.success", "data": {"status": "success", "metadata": {}}}
    with session_scope() as session:
        apply_payment_event(session, event)
        customer = session.get(Customer, customer_id)
        assert customer.has_paid is False


def test_irrelevant_event_type_is_ignored(customer_id):
    event = {"event": "subscription.create", "data": {"metadata": {"customer_id": customer_id}}}
    with session_scope() as session:
        apply_payment_event(session, event)
        customer = session.get(Customer, customer_id)
        assert customer.has_paid is False
