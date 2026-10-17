from datetime import datetime, timedelta, timezone

import pytest

from mba_bot.db import init_db, list_active_accounts, session_scope
from mba_bot.models import Customer, TradingAccount


def _make_account(session, *, email, has_paid, account_active):
    customer = Customer(email=email, password_hash="x", has_paid=has_paid)
    session.add(customer)
    session.flush()
    account = TradingAccount(
        customer_id=customer.id,
        pair="BTC/USDT",
        market_type="spot",
        notification_channel="telegram",
        notification_target="123",
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
        is_active=account_active,
    )
    session.add(account)
    session.flush()
    return account.id


@pytest.fixture()
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def test_new_unpaid_account_is_still_scheduled(db):
    # Payment is no longer a scheduler-level filter - an unpaid account
    # still gets a tick every poll, it just never leaves PAPER, because
    # resolve_trading_mode (onboarding.py) enforces has_paid, not this
    # query. See db.list_active_accounts.
    with session_scope() as session:
        account_id = _make_account(session, email="brand-new@example.com", has_paid=False, account_active=True)

    with session_scope() as session:
        assert account_id in {a.id for a in list_active_accounts(session)}


def test_unpaid_and_paid_accounts_are_both_scheduled(db):
    with session_scope() as session:
        unpaid_id = _make_account(session, email="unpaid@example.com", has_paid=False, account_active=True)
        paid_id = _make_account(session, email="paid@example.com", has_paid=True, account_active=True)

    with session_scope() as session:
        ids = {a.id for a in list_active_accounts(session)}
        assert paid_id in ids
        assert unpaid_id in ids


def test_paused_account_is_excluded_regardless_of_payment(db):
    with session_scope() as session:
        paid_paused_id = _make_account(session, email="paused-paid@example.com", has_paid=True, account_active=False)
        unpaid_paused_id = _make_account(
            session, email="paused-unpaid@example.com", has_paid=False, account_active=False
        )

    with session_scope() as session:
        ids = {a.id for a in list_active_accounts(session)}
        assert paid_paused_id not in ids
        assert unpaid_paused_id not in ids


def test_paid_and_active_account_is_included(db):
    with session_scope() as session:
        account_id = _make_account(session, email="unlocked@example.com", has_paid=True, account_active=True)

    with session_scope() as session:
        assert account_id in {a.id for a in list_active_accounts(session)}
