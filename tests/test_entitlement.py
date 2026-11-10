"""Subscription billing, stage 0: the entitlement layer (billing/entitlement.py, docs/security/billing_entitlement.md).

No provider code exists yet, so nothing here touches a payment provider. What is proven:
  * with SUBSCRIPTION_BILLING_ENABLED off (the default), is_entitled is EXACTLY Customer.has_paid and never reads the
    subscription; every other test file in the suite passing unchanged is the wider proof;
  * with it on, the entitlement matrix at every boundary, to the second;
  * expiry only decides NEW entries: a live position open at expiry is still managed to its exit, and nothing is
    closed, cancelled or deleted;
  * the API answers follow the same rule.
The fake exchange proves control flow, not Binance behaviour.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot.billing.entitlement import EntitlementState, entitlement_state, grace_end, is_entitled
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.models import Customer, Subscription, Trade, TradingAccount

T0 = datetime(2026, 12, 1, 12, 0, 0, tzinfo=timezone.utc)  # a period end
DAY = timedelta(days=1)
SEC = timedelta(seconds=1)


def settings_for(on: bool, grace: int = 5):
    return SimpleNamespace(subscription_billing_enabled=on, subscription_grace_days=grace)


def sub(status="active", end=T0, grace_until=None):
    return SimpleNamespace(status=status, current_period_end=end, grace_until=grace_until)


def cust(has_paid, subscription=None):
    return SimpleNamespace(has_paid=has_paid, subscription=subscription)


class NoSubscriptionRead:
    """A customer whose subscription must never be read."""

    def __init__(self, has_paid):
        self.has_paid = has_paid

    @property
    def subscription(self):
        raise AssertionError("the flag is off: the subscription must not be read")


# ------------------------------------------------------------------ the switch and its parsing


def test_the_switch_defaults_off_and_grace_defaults_to_five_days(monkeypatch):
    monkeypatch.delenv("SUBSCRIPTION_BILLING_ENABLED", raising=False)
    monkeypatch.delenv("SUBSCRIPTION_GRACE_DAYS", raising=False)
    s = load_settings()
    assert s.subscription_billing_enabled is False and s.subscription_grace_days == 5


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("on", True), ("0", False), ("off", False), ("", False), ("maybe", False)])
def test_the_switch_parses_like_every_other_switch(monkeypatch, raw, expected):
    monkeypatch.setenv("SUBSCRIPTION_BILLING_ENABLED", raw)
    assert load_settings().subscription_billing_enabled is expected


@pytest.mark.parametrize("raw,expected", [("7", 7), ("0", 0), ("-3", 0), ("99", 60), ("abc", 5), ("", 5)])
def test_grace_days_are_clamped_and_a_malformed_value_means_the_default(monkeypatch, raw, expected):
    monkeypatch.setenv("SUBSCRIPTION_GRACE_DAYS", raw)
    assert load_settings().subscription_grace_days == expected


# ------------------------------------------------------------------ flag off: exactly has_paid


@pytest.mark.parametrize("has_paid", [True, False])
@pytest.mark.parametrize("sub_state", [None, "active", "cancelled", "expired", "garbage"])
def test_flag_off_is_exactly_has_paid_whatever_the_subscription_says(has_paid, sub_state):
    subscription = None if sub_state is None else sub(status=sub_state, end=T0 - 400 * DAY)
    assert is_entitled(cust(has_paid, subscription), settings=settings_for(False)) is has_paid
    assert is_entitled(cust(has_paid, subscription), settings=settings_for(False), now=T0 + 999 * DAY) is has_paid


@pytest.mark.parametrize("has_paid", [True, False])
def test_flag_off_never_reads_the_subscription_at_all(has_paid):
    assert is_entitled(NoSubscriptionRead(has_paid), settings=settings_for(False)) is has_paid
    assert entitlement_state(NoSubscriptionRead(has_paid), settings=settings_for(False)) in (EntitlementState.LEGACY_LIFETIME, EntitlementState.NONE)


def test_has_paid_that_is_not_a_bool_still_returns_a_bool():
    assert is_entitled(cust(1), settings=settings_for(False)) is True
    assert is_entitled(cust(None), settings=settings_for(False)) is False


# ------------------------------------------------------------------ flag on: the matrix


ON = settings_for(True, grace=5)


def test_legacy_lifetime_customers_keep_access_for_ever_and_unpaid_without_a_row_do_not():
    assert is_entitled(cust(True), settings=ON, now=T0 + 10_000 * DAY) is True  # D1
    assert entitlement_state(cust(True), settings=ON) is EntitlementState.LEGACY_LIFETIME
    assert is_entitled(cust(False), settings=ON, now=T0) is False
    assert entitlement_state(cust(False), settings=ON) is EntitlementState.NONE


@pytest.mark.parametrize(
    "offset,state,entitled",
    [
        (-DAY, EntitlementState.ACTIVE, True),
        (-SEC, EntitlementState.ACTIVE, True),  # one second before the period ends
        (timedelta(0), EntitlementState.GRACE, True),  # the period ends AT the end (exclusive): grace begins
        (SEC, EntitlementState.GRACE, True),
        (5 * DAY - SEC, EntitlementState.GRACE, True),  # last second of grace
        (5 * DAY, EntitlementState.EXPIRED, False),  # grace ends AT end + 5 days (exclusive)
        (5 * DAY + SEC, EntitlementState.EXPIRED, False),
        (400 * DAY, EntitlementState.EXPIRED, False),
    ],
)
def test_an_active_subscription_around_its_period_end(offset, state, entitled):
    c = cust(False, sub("active"))
    assert entitlement_state(c, settings=ON, now=T0 + offset) is state
    assert is_entitled(c, settings=ON, now=T0 + offset) is entitled


@pytest.mark.parametrize("offset,state,entitled", [(-SEC, EntitlementState.CANCELLED, True), (timedelta(0), EntitlementState.EXPIRED, False), (DAY, EntitlementState.EXPIRED, False)])
def test_a_cancelled_subscription_is_entitled_until_the_period_end_and_gets_no_grace(offset, state, entitled):
    c = cust(False, sub("cancelled"))
    assert entitlement_state(c, settings=ON, now=T0 + offset) is state
    assert is_entitled(c, settings=ON, now=T0 + offset) is entitled


@pytest.mark.parametrize("status", ["expired", "pending", "garbage", "", None])
def test_an_unknown_or_expired_status_is_never_entitled_even_inside_the_period(status):
    assert is_entitled(cust(False, sub(status)), settings=ON, now=T0 - 10 * DAY) is False


def test_a_subscription_without_a_period_end_fails_closed():
    assert is_entitled(cust(False, sub("active", end=None)), settings=ON, now=T0) is False
    assert is_entitled(cust(True, sub("active", end=None)), settings=ON, now=T0) is False  # the row governs, not has_paid


def test_a_row_governs_when_it_exists_even_for_a_customer_who_also_has_has_paid():
    c = cust(True, sub("active"))
    assert is_entitled(c, settings=ON, now=T0 + 6 * DAY) is False
    assert is_entitled(c, settings=ON, now=T0 - DAY) is True


def test_grace_length_comes_from_the_setting_and_zero_means_none():
    c = cust(False, sub("active"))
    assert is_entitled(c, settings=settings_for(True, grace=0), now=T0) is False
    assert is_entitled(c, settings=settings_for(True, grace=2), now=T0 + 2 * DAY - SEC) is True
    assert is_entitled(c, settings=settings_for(True, grace=2), now=T0 + 2 * DAY) is False


def test_a_stored_grace_until_extends_grace_but_can_never_end_it_before_the_period_ends():
    later = T0 + 9 * DAY
    assert grace_end(sub("active", grace_until=later), 5) == later
    assert is_entitled(cust(False, sub("active", grace_until=later)), settings=ON, now=T0 + 8 * DAY) is True
    earlier = T0 - 3 * DAY  # nonsense value
    assert grace_end(sub("active", grace_until=earlier), 5) == T0
    assert is_entitled(cust(False, sub("active", grace_until=earlier)), settings=ON, now=T0 + SEC) is False


def test_naive_datetimes_from_sqlite_are_read_as_utc():
    naive_end = T0.replace(tzinfo=None)
    c = cust(False, sub("active", end=naive_end))
    assert is_entitled(c, settings=ON, now=T0 - SEC) is True
    assert is_entitled(c, settings=ON, now=T0 + 5 * DAY) is False


def test_every_combination_flag_on_matches_the_table_for_a_sweep_of_times():
    for status, offset in itertools.product(["active", "cancelled"], range(-3, 9)):
        now = T0 + offset * DAY
        got = is_entitled(cust(False, sub(status)), settings=ON, now=now)
        want = (now < T0) if status == "cancelled" else (now < T0 + 5 * DAY)
        assert got is want, (status, offset)


# ------------------------------------------------------------------ the real model and database


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/ent.db")


def _customer_with_subscription(session, *, has_paid, status="active", end=None):
    c = Customer(email=f"c{abs(hash((has_paid, status, str(end))))}@example.com", password_hash="x", has_paid=has_paid)
    session.add(c)
    session.flush()
    if status is not None:
        session.add(Subscription(customer_id=c.id, status=status, current_period_start=T0 - 30 * DAY, current_period_end=end or T0, provider="manual"))
        session.flush()
        session.refresh(c)
    return c


def test_the_relationship_loads_the_row_and_the_function_reads_it(db):
    with session_scope() as session:
        c = _customer_with_subscription(session, has_paid=False)
        assert c.subscription is not None and c.subscription.status == "active"
        assert is_entitled(c, settings=ON, now=T0 - SEC) is True
        assert is_entitled(c, settings=ON, now=T0 + 6 * DAY) is False
        legacy = _customer_with_subscription(session, has_paid=True, status=None)
        assert legacy.subscription is None and is_entitled(legacy, settings=ON, now=T0 + 3000 * DAY) is True


def test_one_subscription_per_customer(db):
    from sqlalchemy.exc import IntegrityError

    with session_scope() as session:
        c = _customer_with_subscription(session, has_paid=False)
        customer_id = c.id
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(Subscription(customer_id=customer_id, status="active", current_period_end=T0))
            session.flush()
    with session_scope() as session:
        assert session.query(Subscription).filter(Subscription.customer_id == customer_id).count() == 1


# ------------------------------------------------------------------ the trading path


def _set_subscription(account_id, status, end):
    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        session.add(Subscription(customer_id=account.customer_id, status=status, current_period_start=end - 30 * DAY, current_period_end=end, provider="manual"))


def _flag_on(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_BILLING_ENABLED", "1")


def _flag_settings(monkeypatch, on: bool):
    if on:
        monkeypatch.setenv("SUBSCRIPTION_BILLING_ENABLED", "1")
    else:
        monkeypatch.delenv("SUBSCRIPTION_BILLING_ENABLED", raising=False)
    # Paper mode reads the shared platform practice keys (trader._resolve_credentials), so give the test settings placeholders.
    return make_settings(max_notional_multiplier=100.0, testnet_spot_api_key="placeholder-key", testnet_spot_api_secret="placeholder-secret")


def test_flag_off_a_paid_customer_trades_live_exactly_as_before(db, monkeypatch):
    settings = _flag_settings(monkeypatch, False)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="spot")
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade is not None and trade.is_paper is False


def test_flag_on_an_expired_subscriber_gets_no_new_live_entry_only_paper(db, monkeypatch):
    settings = _flag_settings(monkeypatch, True)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="spot")
    now = datetime.now(timezone.utc)
    _set_subscription(account_id, "active", now - 30 * DAY)  # ended 30 days ago: past grace
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade is not None and trade.is_paper is True
    assert not any(call[0] == "create_order" for call in client.calls)  # nothing reached the exchange


def test_flag_on_a_subscriber_in_grace_still_trades_live(db, monkeypatch):
    settings = _flag_settings(monkeypatch, True)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="spot")
    now = datetime.now(timezone.utc)
    _set_subscription(account_id, "active", now - 2 * DAY)  # ended 2 days ago: inside the 5 day grace
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade is not None and trade.is_paper is False


def test_flag_on_a_legacy_lifetime_customer_with_no_row_still_trades_live(db, monkeypatch):
    settings = _flag_settings(monkeypatch, True)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="spot")  # has_paid True, no subscription row
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade is not None and trade.is_paper is False


def test_an_open_live_position_is_still_managed_after_the_subscription_expires(db, monkeypatch):
    """Expiry decides new entries only. The position keeps its protection, is not closed, not orphaned and nothing is deleted."""
    settings = _flag_settings(monkeypatch, False)
    client, settings, account_id = open_protected(monkeypatch, settings=settings)
    before = the_trade(account_id)
    assert before.is_paper is False and before.status == "open"
    _flag_on(monkeypatch)
    _set_subscription(account_id, "active", datetime.now(timezone.utc) - 60 * DAY)  # long expired
    run_cycle(account_id, settings)
    after = the_trade(account_id)
    assert after.id == before.id and after.is_paper is False
    assert after.status == "open" and after.protection_status == "protected"  # not closed, still protected
    with session_scope() as session:  # nothing deleted: the account, its credential and the trade are all there
        account = session.get(TradingAccount, account_id)
        assert account is not None and account.credential is not None
        assert session.query(Trade).filter(Trade.account_id == account_id).count() == 1


# ------------------------------------------------------------------ the API


def _api_client(monkeypatch, on):
    import os
    import tempfile

    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/entitlement_api.db")
    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
    os.environ.setdefault("COOKIE_SECURE", "false")
    os.environ.setdefault("POLL_INTERVAL_SECONDS", "3600")
    from mba_bot.api.app import app

    if on:
        monkeypatch.setenv("SUBSCRIPTION_BILLING_ENABLED", "1")
    else:
        monkeypatch.delenv("SUBSCRIPTION_BILLING_ENABLED", raising=False)
    return TestClient(app, base_url="https://testserver")


def _signup(client, email):
    r = client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True})
    assert r.status_code == 201, r.text
    created = client.post("/api/account", json={"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "api_key": "k", "api_secret": "s"})
    assert created.status_code == 201, created.text
    return r.json()["id"]


def _give_subscription(customer_id, status, end):
    with session_scope() as session:
        session.add(Subscription(customer_id=customer_id, status=status, current_period_start=end - 30 * DAY, current_period_end=end, provider="manual"))


def test_flag_off_the_api_reports_has_paid_as_stored_and_ignores_a_subscription_row(monkeypatch):
    with _api_client(monkeypatch, False) as client:
        cid = _signup(client, "ent-off@example.com")
        _give_subscription(cid, "active", datetime.now(timezone.utc) + 20 * DAY)  # would entitle, if the flag were on
        me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["has_paid"] is False


def test_flag_on_the_api_follows_the_entitlement_rule(monkeypatch):
    now = datetime.now(timezone.utc)
    with _api_client(monkeypatch, True) as client:
        cid = _signup(client, "ent-on@example.com")
        assert client.get("/api/auth/me").json()["has_paid"] is False  # no row, not has_paid
        _give_subscription(cid, "active", now + 20 * DAY)
        assert client.get("/api/auth/me").json()["has_paid"] is True
        denied_before = client.put("/api/account/credentials", json={"api_key": "k", "api_secret": "s", "environment": "mainnet"})
        with session_scope() as session:
            sub_row = session.query(Subscription).filter(Subscription.customer_id == cid).one()
            sub_row.current_period_end = now - 30 * DAY  # now long past grace
        assert client.get("/api/auth/me").json()["has_paid"] is False
        denied_after = client.put("/api/account/credentials", json={"api_key": "k", "api_secret": "s", "environment": "mainnet"})
    assert denied_before.status_code != 403  # entitled: the gate does not stop it here (other checks may)
    assert denied_after.status_code == 403 and "pay to unlock" in denied_after.text


# ------------------------------------------------------------------ the additive migration


def test_the_migration_adds_two_tables_touches_nothing_else_and_is_idempotent(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = next((Path(__file__).resolve().parent.parent / "alembic" / "versions").glob("f6c3a9d1b8e2_*.py"))
    spec = importlib.util.spec_from_file_location("billing_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "e5b9c2d7f1a4"

    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    with eng.begin() as conn:
        conn.execute(text("create table customers (id varchar(36) primary key, email varchar(255), has_paid boolean)"))
        conn.execute(text("insert into customers values ('c1', 'a@example.test', 1)"))

    def run(fn):
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    def tables():
        return sorted(inspect(eng).get_table_names())

    run(mig.upgrade)
    assert tables() == ["billing_payments", "customers", "subscriptions"]
    run(mig.upgrade)  # create_all() may already have made them: running again must not fail or change anything
    assert tables() == ["billing_payments", "customers", "subscriptions"]
    with eng.connect() as conn:
        assert conn.execute(text("select id, email, has_paid from customers")).all() == [("c1", "a@example.test", 1)]  # no backfill, no change
        assert conn.execute(text("select count(*) from subscriptions")).scalar() == 0
    assert [c["name"] for c in inspect(eng).get_columns("customers")] == ["id", "email", "has_paid"]
    run(mig.downgrade)
    assert tables() == ["customers"]


# ------------------------------------------------------------------ nothing bypasses the layer


def test_no_code_reads_customer_has_paid_outside_the_entitlement_layer():
    """Every decision about who may open live trades goes through is_entitled. The only places that may touch the
    has_paid attribute are the entitlement function itself, the model, and the payment router that WRITES it."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "src" / "mba_bot"
    allowed = {root / "models.py", root / "billing" / "entitlement.py", root / "api" / "routers" / "billing.py"}
    offenders = []
    for path in root.rglob("*.py"):
        if path in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr == "has_paid":
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], offenders
