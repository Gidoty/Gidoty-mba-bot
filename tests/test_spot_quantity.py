"""Spot protectable-quantity resolution: only exchange-confirmed facts.

protectable = floor_to_lot_step(min(bot-attributable net fill, FREE base))
  net fill = filled - base-asset commission, where the commission comes from
  the order response, else the account's own fill records for that order.
If neither can be established the bot takes the emergency path; it never
assumes a fee rate. The wallet may hold the customer's own units of the same
asset, so the balance is only ever an upper bound, never the quantity."""
from __future__ import annotations

import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot.db import init_db


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _enter(monkeypatch, *, pre_existing=0.0, **client_kw):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(**client_kw)
    if pre_existing:
        client.free["BTC"] = pre_existing
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    return client, settings, account_id


def _stop(client):
    return client.orders_by_role("stop")[0]


# ---- commission reported in the BASE asset ----


def test_commission_in_base_asset_is_subtracted(db, monkeypatch):
    client, _, account_id = _enter(monkeypatch, fee_in="base")
    entry = client.orders_by_role("entry")[0]
    fee = entry["fees"][0]["cost"]
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.entry_fee_base == pytest.approx(fee)
    assert _stop(client)["amount"] <= entry["filled"] - fee + 1e-12
    assert _stop(client)["amount"] == pytest.approx(trade.quantity)


# ---- commission reported in the QUOTE asset (or BNB): base is intact ----


@pytest.mark.parametrize("asset", ["quote", "bnb"])
def test_commission_in_another_asset_leaves_the_whole_fill_protectable(db, monkeypatch, asset):
    client, _, account_id = _enter(monkeypatch, fee_in=asset)
    entry = client.orders_by_role("entry")[0]
    trade = the_trade(account_id)
    assert trade.entry_fee_base == 0.0 and trade.status == "open"
    assert _stop(client)["amount"] == pytest.approx(entry["filled"], abs=client.step)
    assert _stop(client)["amount"] <= client.free["BTC"] + client.used["BTC"] + 1e-12


# ---- commission information MISSING from the order response ----


def test_missing_commission_in_the_order_is_resolved_from_the_fill_records(db, monkeypatch):
    client, _, account_id = _enter(monkeypatch, fee_info=False)  # order lacks fees; trade records have them
    entry = client.orders_by_role("entry")[0]
    fee = client.fills[0]["fee"]["cost"]
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.entry_fee_base == pytest.approx(fee)
    assert _stop(client)["amount"] <= entry["filled"] - fee + 1e-12
    assert any(c[0] == "fetch_my_trades" for c in client.calls)


def test_missing_commission_everywhere_fails_closed_into_the_emergency_path(db, monkeypatch):
    client, _, account_id = _enter(monkeypatch, fee_info=False, trades_fee_info=False)
    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "quantity_unverifiable"
    assert client.orders_by_role("stop") == [], "no stop is ever sized from an assumed fee"
    exit_order = client.orders_by_role("exit")[0]
    entry = client.orders_by_role("entry")[0]
    assert exit_order["amount"] <= entry["filled"] + 1e-12, "never sells more than the bot's own fill"


def test_missing_commission_and_unreadable_fill_records_also_fail_closed(db, monkeypatch):
    import ccxt.async_support as ccxt

    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(fee_info=False)

    async def broken(*a, **k):
        raise ccxt.RequestTimeout("down")

    client.fetch_my_trades = broken
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.close_reason == "quantity_unverifiable" and client.orders_by_role("stop") == []


def test_fill_records_that_do_not_cover_the_whole_fill_are_not_trusted(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(fee_info=False)
    install(monkeypatch, client)
    real = client.fetch_my_trades

    async def partial(symbol=None, since=None, limit=None, params=None):
        rows = await real(symbol, since, limit, params)
        for r in rows:
            r["amount"] = r["amount"] / 2  # records account for only half the fill
        return rows

    client.fetch_my_trades = partial
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    assert the_trade(account_id).close_reason == "quantity_unverifiable"


def test_a_crash_before_the_quantity_is_resolved_resumes_without_assuming_anything(db, monkeypatch):
    class Crash(BaseException):
        pass

    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(fee_info=False)
    install(monkeypatch, client)
    real = client.fetch_my_trades
    state = {"n": 0}

    async def crash_once(symbol=None, since=None, limit=None, params=None):
        state["n"] += 1
        if state["n"] == 1:
            raise Crash()
        return await real(symbol, since, limit, params)

    client.fetch_my_trades = crash_once
    account_id = new_account(settings)
    with pytest.raises(Crash):
        run_cycle(account_id, settings)
    t = the_trade(account_id)
    assert t.status == "protecting" and t.entry_fee_base is None and client.orders_by_role("stop") == []

    run_reconcile(account_id, settings)
    t = the_trade(account_id)
    assert t.status == "open" and t.entry_fee_base is not None and len(client.orders_by_role("stop")) == 1


# ---- the customer already owns additional units of the same asset ----


def test_preexisting_units_are_never_protected_or_sold(db, monkeypatch):
    client, settings, account_id = _enter(monkeypatch, pre_existing=5.0)
    entry = client.orders_by_role("entry")[0]
    trade = the_trade(account_id)
    fee = entry["fees"][0]["cost"]
    assert _stop(client)["amount"] <= entry["filled"] - fee + 1e-12, "the 5.0 BTC the customer already held is not the bot's"
    assert client.free["BTC"] >= 5.0 - 1e-9, "the customer's own units stay free"

    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    run_cycle(account_id, settings)
    done = the_trade(account_id)
    assert done.status == "closed"
    assert client.orders_by_role("exit")[0]["amount"] <= trade.quantity + 1e-12
    assert client.free["BTC"] >= 5.0 - 1e-9, "the exit sold only the bot's own quantity"


def test_available_balance_larger_than_the_bot_fill_does_not_enlarge_the_stop(db, monkeypatch):
    client, _, account_id = _enter(monkeypatch, pre_existing=1000.0)
    entry = client.orders_by_role("entry")[0]
    assert _stop(client)["amount"] < entry["filled"]
    assert the_trade(account_id).quantity == pytest.approx(_stop(client)["amount"])


# ---- available balance SMALLER than expected ----


def test_available_balance_smaller_than_expected_bounds_the_stop(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings)

    def customer_moves_some_away(fake):
        fake.free["BTC"] -= 0.05  # before the stop is placed, 0.05 BTC leaves the account

    client.before_hooks["stop"] = customer_moves_some_away
    run_cycle(account_id, settings)

    entry = client.orders_by_role("entry")[0]
    fee = entry["fees"][0]["cost"]
    expected_net = entry["filled"] - fee
    trade = the_trade(account_id)
    assert trade.status == "open"
    assert _stop(client)["amount"] <= expected_net - 0.05 + client.step
    assert trade.quantity == pytest.approx(_stop(client)["amount"]), "recorded quantity follows what actually exists"


def test_available_balance_below_the_minimum_means_emergency_not_a_stop(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings)

    def drained(fake):
        fake.free["BTC"] = 0.0004  # below the lot minimum

    client.before_hooks["stop"] = drained
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert client.orders_by_role("stop") == []
    assert trade.status in ("closed", "attention")


# ---- the old fee assumption is gone ----


def test_there_is_no_default_fee_rate_left_in_the_order_layer():
    import inspect

    from mba_bot import lifecycle, venue

    assert not hasattr(venue, "SPOT_DEFAULT_BASE_FEE_RATE")
    assert not hasattr(venue.SpotVenue, "net_quantity")
    for module in (venue, lifecycle):
        assert "0.999" not in inspect.getsource(module) and "0.001" not in inspect.getsource(module)
