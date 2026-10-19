"""Fast protection-health watch for Spot STOP_LOSS_LIMIT positions.

A stop-limit that the market gaps through rests unfilled while looking
protected. The 15-second tick compares each such position's price with its
trigger (one public ticker per symbol, shared by all customers) and, only
for a position at/through its trigger, reconciles the order's true state and
closes the remainder if the price is through the executable limit."""
from __future__ import annotations

import asyncio

import ccxt.async_support as ccxt
import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot import exchange, scheduler
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade

GAPPED = [[i, 30.0, 30.5, 29.5, 30.0, 1.0] for i in range(20)]
LIMIT_ONLY = ["LIMIT", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"]  # no market stop listed


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _setup(monkeypatch, **client_kw):
    """An open, verified stop-limit-protected Spot trade (trigger 43.0, limit ~42.8)."""
    settings = make_settings(max_notional_multiplier=100.0, stop_limit_monitor_interval_seconds=15)
    client = spot_client(order_types=LIMIT_ONLY, **client_kw)
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protective_kind == "stop_loss_limit"
    notifier = FakeNotifier()
    monkeypatch.setattr(scheduler, "build_notifier", lambda *a, **k: notifier)
    return client, settings, account_id, notifier


def _watch(settings):
    asyncio.run(scheduler.monitor_stop_limits_once(settings))


def _names(client, start):
    return [c[0] for c in client.calls[start:]]


# =========================== bounded traffic ===========================


def test_no_stop_limit_positions_means_no_exchange_traffic_at_all(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()  # market STOP_LOSS is listed: not a stop-limit position
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    built = []
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **k: built.append(k) or client)
    start = len(client.calls)

    _watch(settings)

    assert built == [] and len(client.calls) == start, "nothing to watch: no client is even built"


def test_price_above_the_trigger_costs_one_public_ticker_call_and_nothing_else(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    start = len(client.calls)

    _watch(settings)

    assert _names(client, start) == ["fetch_ticker"], "no authenticated call while the price is above the trigger"
    assert the_trade(account_id).status == "open" and notifier.texts == []


def test_many_customers_on_one_symbol_share_one_ticker_call(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    for i in range(3):
        extra = new_account(settings, email=f"extra{i}@example.com")
        run_cycle(extra, settings)
    start = len(client.calls)

    _watch(settings)

    assert _names(client, start).count("fetch_ticker") == 1


# =========================== gap-through recovery ===========================


def test_gap_through_is_closed_within_one_watch_pass_not_five_minutes(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    qty = the_trade(account_id).quantity
    client.move_price(30.0)  # gaps through the 43.0 trigger AND the ~42.8 limit
    stop = client.orders_by_role("stop")[0]
    assert stop["status"] == "open" and stop.get("triggered"), "precondition: looks protected, is not"

    _watch(settings)  # one 15 s pass; no strategy scan, no 300 s wait

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "stop_limit_gap"
    assert stop["status"] == "canceled"
    exits = client.orders_by_role("exit")
    assert len(exits) == 1 and exits[0]["amount"] <= qty + 1e-12
    assert trade.exit_price == pytest.approx(30.0) and trade.pnl < 0
    assert len([t for t in notifier.texts if "Exited" in t]) == 1, "customer notified once"
    assert client.free.get("BTC", 0.0) < client.step and client.used.get("BTC", 0.0) < 1e-9


def test_between_trigger_and_limit_the_exchange_stop_is_left_to_fill(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    client.move_price(42.9)  # through the trigger, still at/above the limit: the fake fills it
    _watch(settings)
    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "exchange_stop_triggered"
    assert client.orders_by_role("exit") == [], "the stop did its job: no competing exit"


def test_triggered_but_unfilled_stop_with_the_price_still_above_the_limit_is_not_touched(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    stop = client.orders_by_role("stop")[0]
    client.price = 42.9
    stop["triggered"] = True  # triggered, resting, limit 42.8 below the price: it can still fill
    _watch(settings)
    assert the_trade(account_id).status == "open" and stop["status"] == "open"
    assert client.orders_by_role("exit") == []


def test_partially_filled_stop_limit_that_the_market_then_gaps_through_closes_only_the_remainder(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    trade = the_trade(account_id)
    stop = client.orders_by_role("stop")[0]
    client.price = 42.9
    client.partially_fill_stop(stop["client_id"], 0.4, 42.9)
    sold_first = stop["filled"]
    client.move_price(30.0)  # then the rest gaps away
    stop["status"] = "open"

    _watch(settings)

    done = the_trade(account_id)
    assert done.status == "closed"
    exit_order = client.orders_by_role("exit")[0]
    assert exit_order["amount"] <= trade.quantity - sold_first + client.step
    assert 30.0 < done.exit_price < 42.9, "blended price of the partial stop fill and the remainder"
    assert stop["status"] == "canceled"
    assert len([t for t in notifier.texts if "Exited" in t]) == 1


# =========================== races and idempotency ===========================


def test_stop_limit_fills_during_the_cancel_so_no_second_sell_is_sent(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    stop = client.orders_by_role("stop")[0]
    client.move_price(30.0)
    client.on_cancel = lambda fake, cid: fake.fill_stop_fully(cid, 42.8)

    _watch(settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "exchange_stop_triggered"
    assert client.orders_by_role("exit") == []
    assert trade.exit_price == pytest.approx(42.8)


def test_watch_and_the_main_loop_firing_together_send_exactly_one_exit(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    client.move_price(30.0)
    client._ohlcv = BEARISH_OHLCV[:-1] + [[len(BEARISH_OHLCV), 30.0, 30.5, 29.5, 30.0, 1.0]]  # also a strategy-exit-ish tape

    async def both():
        return await asyncio.gather(scheduler.monitor_stop_limits_once(settings), scheduler._run_one(account_id, settings))

    asyncio.run(both())

    assert the_trade(account_id).status == "closed"
    assert len(client.orders_by_role("exit")) == 1
    assert len([t for t in notifier.texts if "Exited" in t]) == 1


def test_strategy_exit_and_gap_in_the_same_cycle_do_not_double_exit(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    client.move_price(30.0)
    client._ohlcv = GAPPED
    run_cycle(account_id, settings, notifier)  # main loop sees the gap itself
    _watch(settings)  # and the watch pass finds nothing left to do
    assert len(client.orders_by_role("exit")) == 1 and the_trade(account_id).status == "closed"


def test_lost_exit_response_is_finished_by_the_recovery_loop_with_the_same_order(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    client.move_price(30.0)
    client.fail("create_order", role="exit", exc=ccxt.RequestTimeout("lost"), when="after", times=1)

    _watch(settings)
    assert the_trade(account_id).status == "closing", "exit sent, response lost: the lease is held"

    asyncio.run(scheduler.recover_once(settings))  # the same tick's recovery pass picks it up

    trade = the_trade(account_id)
    assert trade.status == "closed" and len(client.orders_by_role("exit")) == 1
    assert len([t for t in notifier.texts if "Exited" in t]) == 1


def test_exit_that_fails_before_reaching_binance_is_retried_by_the_watch_without_duplicating(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    client.move_price(30.0)
    client.fail("create_order", role="exit", exc=ccxt.RequestTimeout("never arrived"), when="before", times=1)

    _watch(settings)
    assert the_trade(account_id).status == "closing" and client.orders_by_role("exit") == []
    asyncio.run(scheduler.recover_once(settings))

    assert the_trade(account_id).status == "closed" and len(client.orders_by_role("exit")) == 1


def test_exchange_outage_during_the_watch_changes_nothing_and_retries_next_pass(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    client.move_price(30.0)
    real = client.fetch_order

    async def down(*a, **k):
        raise ccxt.NetworkError("outage")

    client.fetch_order = down
    _watch(settings)
    assert the_trade(account_id).status == "open" and client.orders_by_role("exit") == []
    assert client.orders_by_role("stop")[0]["status"] == "open"

    client.fetch_order = real
    _watch(settings)
    assert the_trade(account_id).status == "closed"


def test_a_failed_ticker_read_skips_the_pass_safely(db, monkeypatch):
    client, settings, account_id, notifier = _setup(monkeypatch)
    client.move_price(30.0)

    async def down(symbol):
        raise ccxt.NetworkError("ticker down")

    real = client.fetch_ticker
    client.fetch_ticker = down
    _watch(settings)
    assert the_trade(account_id).status == "open"
    client.fetch_ticker = real
    _watch(settings)
    assert the_trade(account_id).status == "closed"


# =========================== cadence ===========================


@pytest.mark.parametrize("watch_every, expected_watches", [(15, 6), (45, 2), (1, 6)])
def test_watch_cadence_rides_the_recovery_tick_and_never_runs_faster_than_configured(monkeypatch, watch_every, expected_watches):
    settings = make_settings(recovery_interval_seconds=15, stop_limit_monitor_interval_seconds=watch_every)
    ticks = {"n": 0, "watch": 0, "recover": 0}
    clock = {"t": 0.0}

    async def fake_sleep(seconds):
        ticks["n"] += 1
        clock["t"] += seconds
        if ticks["n"] > 6:
            raise asyncio.CancelledError

    async def recover(_settings, **k):
        ticks["recover"] += 1

    async def watch(_settings):
        ticks["watch"] += 1

    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(scheduler, "recover_once", recover)
    monkeypatch.setattr(scheduler, "monitor_stop_limits_once", watch)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run_recovery_forever(settings))

    assert ticks["recover"] == 6 and ticks["watch"] == expected_watches
