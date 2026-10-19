"""Protective-stop lifecycle: entry -> exchange-side stop -> open.

Every exchange behaviour here comes from tests/fake_binance.py and is an
assumption to be confirmed on Binance Testnet, not a Binance fact."""
from __future__ import annotations

import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _new_account(settings, **kw):
    with session_scope() as session:
        return make_account(session, encryption_key=settings.encryption_key, **kw)


def test_spot_entry_places_verified_market_stop_and_goes_open(db, monkeypatch):
    settings = make_settings()
    client = spot_client()
    install(monkeypatch, client)
    account_id = _new_account(settings)

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert trade.protective_kind == "stop_loss"
    stops = client.orders_by_role("stop")
    assert len(stops) == 1 and stops[0]["status"] == "open" and stops[0]["conditional"]
    # the stop covers the NET base quantity (entry fee was taken in base), never more
    assert stops[0]["amount"] <= client.used["BTC"] + 1e-9
    assert stops[0]["amount"] < client.orders_by_role("entry")[0]["filled"]
    assert any("resting at Binance" in t for t in notifier.texts)
    assert trade.entry_order_id is not None and trade.protective_order_id is not None


def test_futures_entry_places_close_position_stop_and_goes_open(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client()
    install(monkeypatch, client)
    account_id = _new_account(settings, market_type="futures")

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert trade.protective_kind == "stop_market_close"
    stop = client.orders_by_role("stop")[0]
    assert stop["conditional"] and stop["close_position"] and stop["side"] == "sell"
    assert client.position > 0 and client.violations == []


# --- helpers ---------------------------------------------------------------------------


def _open_protected(monkeypatch, *, market="spot", settings=None, **client_kw):
    settings = settings or make_settings(max_notional_multiplier=100.0)
    client = (spot_client if market == "spot" else futures_client)(**client_kw)
    install(monkeypatch, client)
    account_id = _new_account(settings, market_type=market)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    return client, settings, account_id


def _exit_orders(client):
    return client.orders_by_role("exit")


# --- stop triggered at the exchange ------------------------------------------------------


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_exchange_stop_fires_and_the_bot_records_it_without_sending_any_exit(db, monkeypatch, market):
    client, settings, account_id = _open_protected(monkeypatch, market=market)
    entry_qty = the_trade(account_id).quantity

    client.move_price(40.0)  # through the 43.0 stop, while the bot is "offline"
    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "exchange_stop_triggered"
    assert trade.protection_status == "triggered"
    assert trade.exit_price == pytest.approx(40.0, abs=0.01) and trade.pnl < 0
    assert trade.pnl == pytest.approx((trade.exit_price - trade.entry_price) * entry_qty, rel=1e-6)
    assert _exit_orders(client) == [], "the stop already closed it: the bot must not send an exit on top"
    assert client.violations == []
    assert any("stop-loss triggered at Binance" in t for t in notifier.texts)


def test_after_the_stop_fires_the_account_returns_to_scanning(db, monkeypatch):
    client, settings, account_id = _open_protected(monkeypatch, market="spot")
    client.move_price(40.0)
    run_cycle(account_id, settings)  # records the stop-out
    client._ohlcv = FLAT_OHLCV
    client.price = 100.0
    run_cycle(account_id, settings)  # flat market: scanning, nothing opened
    assert [t.status for t in all_trades(account_id)] == ["closed"]


# --- strategy exit before the stop ---------------------------------------------------------


def test_spot_crossover_exit_cancels_the_stop_first_then_sells_only_the_net_quantity(db, monkeypatch):
    client, settings, account_id = _open_protected(monkeypatch, market="spot")
    held = client.used["BTC"]
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "crossover_exit"
    assert client.orders_by_role("stop")[0]["status"] == "canceled"
    exits = _exit_orders(client)
    assert len(exits) == 1 and exits[0]["amount"] <= held + 1e-9
    names = [c[0] for c in client.calls]
    assert names.index("cancel_order") < max(i for i, n in enumerate(names) if n == "create_order"), "cancel before exit"
    assert client.used.get("BTC", 0.0) == pytest.approx(0.0)
    assert any("crossover exit" in t for t in notifier.texts)


def test_futures_crossover_exit_is_reduce_only_and_leaves_the_account_flat(db, monkeypatch):
    client, settings, account_id = _open_protected(monkeypatch, market="futures")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "crossover_exit"
    assert client.position == 0.0 and client.violations == []
    assert client.orders_by_role("stop")[0]["status"] == "canceled"
    assert len(_exit_orders(client)) == 1


# --- cancellation race ------------------------------------------------------------------------


def test_spot_stop_fires_during_the_cancel_so_the_bot_does_not_also_sell(db, monkeypatch):
    client, settings, account_id = _open_protected(monkeypatch, market="spot")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    # The instant the bot asks to cancel, the market drops through the stop.
    client.on_cancel = lambda fake, cid: fake.move_price(40.0)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "exchange_stop_triggered"
    assert _exit_orders(client) == [], "stop-then-software-exit double execution"
    assert client.free.get("BTC", 0.0) < client.step  # only sub-lot dust remains


def test_futures_stop_fires_during_the_cancel_so_the_bot_does_not_reverse_the_position(db, monkeypatch):
    client, settings, account_id = _open_protected(monkeypatch, market="futures")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    client.on_cancel = lambda fake, cid: fake.move_price(40.0)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "exchange_stop_triggered"
    assert client.position == 0.0 and client.violations == []
    assert _exit_orders(client) == []


# --- Futures reduce-only / flat double exit ------------------------------------------------------


def test_a_second_exit_on_an_already_flat_futures_position_sends_nothing(db, monkeypatch):
    client, settings, account_id = _open_protected(monkeypatch, market="futures")
    client.externally_close_position()  # customer closes it by hand
    before = len(client.orders)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "externally_closed"
    assert len(client.orders) == before and client.position == 0.0 and client.violations == []


def test_exit_never_flips_a_futures_position_even_if_the_exchange_holds_more_than_the_bot_expects(db, monkeypatch):
    client, settings, account_id = _open_protected(monkeypatch, market="futures")
    qty = the_trade(account_id).quantity
    client.position = qty * 3  # customer added to the position outside the bot
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE

    run_cycle(account_id, settings)

    exit_order = _exit_orders(client)[0]
    assert exit_order["reduce_only"] and exit_order["amount"] <= qty + 1e-9
    assert client.violations == []
    assert client.position > 0, "only the bot's own quantity may be closed, never the customer's remainder"
