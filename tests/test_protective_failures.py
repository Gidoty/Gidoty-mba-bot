"""Fault injection around the entry -> protection -> exit lifecycle:
lost responses, timeouts, rejections, crashes at every checkpoint, DB
failures, races and restarts.

The exchange is tests/fake_binance.py. Passing here shows the bot's state
machine is safe under these faults GIVEN the modelled Binance behaviour; it
does not show Binance behaves that way (see ARCHITECTURE.md, Testnet
validation procedure)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest
from fake_binance import FakeBinance
from helpers_live import *  # noqa: F401,F403
from sqlalchemy.exc import OperationalError

from mba_bot import lifecycle
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade, TradingAccount


class Crash(BaseException):
    """Simulates the process dying at an exact point (not catchable by `except Exception`)."""


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _stop_calls(client):
    return [c for c in client.calls if c[0] == "create_order" and "stopLossPrice" in c[6]]


def _age_trade(account_id, seconds):
    with session_scope() as session:
        t = session.query(Trade).filter(Trade.account_id == account_id).one()
        t.opened_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)


# =========================== protection rejected / unavailable ===========================


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_protection_rejected_means_immediate_emergency_close_not_a_normal_open_trade(db, monkeypatch, market):
    settings = make_settings(max_notional_multiplier=100.0)
    client = (spot_client if market == "spot" else futures_client)()
    client.fail("create_order", role="stop", exc=ccxt.InvalidOrder("rejected"), times=99)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type=market)

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "protection_failed_emergency_exit"
    assert len(_stop_calls(client)) <= 2, "a definitive rejection must not be hammered with retries"
    exits = client.orders_by_role("exit")
    assert len(exits) == 1
    if market == "futures":
        assert exits[0]["reduce_only"] and client.position == 0.0 and client.violations == []
    else:
        assert client.free.get("BTC", 0.0) < client.step
    assert any("Safety exit" in t for t in notifier.texts)
    assert not any("resting at Binance" in t for t in notifier.texts), "never announced as a protected trade"


def test_transient_stop_failures_are_retried_within_bounds_then_succeed(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    client.fail("create_order", role="stop", exc=ccxt.RequestTimeout("t"), times=2)
    install(monkeypatch, client)
    account_id = new_account(settings)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert len(client.orders_by_role("stop")) == 1


def test_persistent_transient_stop_failures_end_in_emergency_close(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    client.fail("create_order", role="stop", exc=ccxt.RequestTimeout("t"), times=99)
    install(monkeypatch, client)
    account_id = new_account(settings)

    run_cycle(account_id, settings)

    assert len(_stop_calls(client)) == settings.protection_max_attempts
    assert the_trade(account_id).close_reason == "protection_failed_emergency_exit"


def test_lost_stop_response_does_not_create_a_second_stop(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    client.fail("create_order", role="stop", exc=ccxt.RequestTimeout("lost"), when="after", times=1)
    install(monkeypatch, client)
    account_id = new_account(settings)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert len(client.orders_by_role("stop")) == 1, "the lost-response stop was adopted, not duplicated"


# =========================== entry uncertainty ===========================


def test_entry_timeout_before_exchange_acceptance_leaves_it_pending_and_blocks_a_second_entry(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    client.fail("create_order", role="entry", exc=ccxt.RequestTimeout("never arrived"), when="before", times=1)
    install(monkeypatch, client)
    account_id = new_account(settings)

    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "pending"
    assert client.orders_by_role("entry") == []

    run_cycle(account_id, settings)  # signal still bullish: must NOT enter again while this is unresolved
    run_cycle(account_id, settings)
    assert len(all_trades(account_id)) == 1 and client.orders_by_role("entry") == []

    # Only once it is old enough that it cannot still be in flight is it voided...
    _age_trade(account_id, 600)
    run_cycle(account_id, settings)
    assert [t.status for t in all_trades(account_id)].count("void") == 1
    # ...after which the next cycle may enter normally.
    run_cycle(account_id, settings)
    statuses = sorted(t.status for t in all_trades(account_id))
    assert statuses == ["open", "void"]
    assert len(client.orders_by_role("entry")) == 1


def test_entry_timeout_after_exchange_acceptance_is_adopted_and_protected_without_a_second_order(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    client.fail("create_order", role="entry", exc=ccxt.RequestTimeout("response lost"), when="after", times=1)
    install(monkeypatch, client)
    account_id = new_account(settings)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert len(client.orders_by_role("entry")) == 1


def test_lost_entry_response_and_unreadable_lookup_stays_pending_then_recovers_on_the_next_cycle(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    client.fail("create_order", role="entry", exc=ccxt.RequestTimeout("lost"), when="after", times=1)
    client.fail("fetch_order", exc=ccxt.RequestTimeout("lookup down"), times=1)
    install(monkeypatch, client)
    account_id = new_account(settings)

    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "pending"
    assert len(client.orders_by_role("entry")) == 1

    run_cycle(account_id, settings)  # reconcile finds the order by client id
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert len(client.orders_by_role("entry")) == 1


def test_definitive_entry_rejection_voids_the_trade_and_leaves_no_exposure(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    client.fail("create_order", role="entry", exc=ccxt.InsufficientFunds("no"), times=1)
    install(monkeypatch, client)
    account_id = new_account(settings)

    with pytest.raises(ccxt.InsufficientFunds):
        run_cycle(account_id, settings)

    assert the_trade(account_id).status == "void"
    assert client.orders == {}


def test_invalid_credentials_on_entry_void_the_trade_and_record_a_safe_error(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    client.fail("create_order", role="entry", exc=ccxt.AuthenticationError("-2015 secret detail"), times=1)
    install(monkeypatch, client)
    account_id = new_account(settings)

    run_cycle(account_id, settings, swallow=True)

    assert the_trade(account_id).status == "void"
    with session_scope() as session:
        message = session.get(TradingAccount, account_id).last_error
    assert "rejected this account's API key" in message and "secret detail" not in message


def test_partial_entry_fill_protects_exactly_what_filled_spot(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(min_notional=0.1)
    client.partial_entry_fill = 0.5
    install(monkeypatch, client)
    account_id = new_account(settings)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    entry = client.orders_by_role("entry")[0]
    assert entry["status"] == "expired" and 0 < entry["filled"] < entry["amount"]
    assert trade.status == "open"
    stop = client.orders_by_role("stop")[0]
    assert stop["amount"] <= entry["filled"] and trade.quantity == pytest.approx(stop["amount"])


def test_a_partial_spot_fill_too_small_to_stop_but_big_enough_to_sell_is_sold_not_left_naked(db, monkeypatch):
    # 50% fill = ~0.105 BTC: ~9.5 USDT at market (sellable) but only ~4.5 at the 43 stop (< 5 minNotional).
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(min_notional=5.0)
    client.partial_entry_fill = 0.5
    install(monkeypatch, client)
    account_id = new_account(settings)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "protection_failed_emergency_exit"
    assert client.orders_by_role("stop") == [] and len(client.orders_by_role("exit")) == 1
    assert client.free.get("BTC", 0.0) < client.step


def test_partial_entry_fill_protects_exactly_what_filled_futures(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client()
    client.partial_entry_fill = 0.5
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    entry = client.orders_by_role("entry")[0]
    assert trade.status == "open" and trade.quantity == pytest.approx(entry["filled"])
    stop = client.orders_by_role("stop")[0]
    assert stop["close_position"], "closePosition sizes itself to whatever actually filled"


# =========================== DB write failure after exchange success ===========================


def test_db_failure_after_the_entry_filled_is_recovered_without_a_duplicate_entry(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings)
    real_cas = lifecycle._cas
    state = {"failed": False}

    def flaky(session, trade, expected, new, **fields):
        if new == "protecting" and not state["failed"]:
            state["failed"] = True
            raise OperationalError("UPDATE", {}, Exception("disk I/O error"))
        return real_cas(session, trade, expected, new, **fields)

    monkeypatch.setattr(lifecycle, "_cas", flaky)

    with pytest.raises(OperationalError):
        run_cycle(account_id, settings)
    assert the_trade(account_id).status == "pending", "the write-ahead row survived the failed fill update"
    assert len(client.orders_by_role("entry")) == 1

    run_cycle(account_id, settings)  # restart: reconcile adopts the order Binance already holds
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert len(client.orders_by_role("entry")) == 1


# =========================== crash at every checkpoint, then restart ===========================


def test_crash_right_after_the_entry_order_is_recovered_on_restart(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    client.fail("create_order", role="entry", exc=Crash(), when="after", times=1)
    install(monkeypatch, client)
    account_id = new_account(settings)

    with pytest.raises(Crash):
        run_cycle(account_id, settings)
    assert the_trade(account_id).status == "pending" and len(client.orders_by_role("entry")) == 1

    run_reconcile(account_id, settings)  # startup reconciliation
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert len(client.orders_by_role("entry")) == 1 and len(client.orders_by_role("stop")) == 1


def test_crash_right_after_the_stop_was_placed_is_adopted_on_restart(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client()
    client.fail("create_order", role="stop", exc=Crash(), when="after", times=1)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    with pytest.raises(Crash):
        run_cycle(account_id, settings)
    assert the_trade(account_id).status == "protecting"

    run_reconcile(account_id, settings)
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert len(client.orders_by_role("stop")) == 1 and client.violations == []


def test_crash_after_the_exit_order_filled_does_not_exit_twice_on_restart(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="futures")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    client.fail("create_order", role="exit", exc=Crash(), when="after", times=1)

    with pytest.raises(Crash):
        run_cycle(account_id, settings)
    assert the_trade(account_id).status == "closing" and client.position == 0.0

    run_reconcile(account_id, settings)
    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "crossover_exit"
    assert len(client.orders_by_role("exit")) == 1 and client.violations == []


def test_lost_exit_response_is_resolved_by_client_id_without_a_second_exit(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="spot")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    client.fail("create_order", role="exit", exc=ccxt.RequestTimeout("lost"), when="after", times=1)

    run_cycle(account_id, settings)  # exit placed, response lost -> deferred
    assert the_trade(account_id).status == "closing"
    run_cycle(account_id, settings)  # next pass finds the exit by its client id

    assert the_trade(account_id).status == "closed"
    assert len(client.orders_by_role("exit")) == 1


def test_restart_with_a_healthy_protected_open_trade_changes_nothing_and_sends_no_message(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="spot")
    orders_before = len(client.orders)

    notifier = run_reconcile(account_id, settings)
    notifier2 = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and len(client.orders) == orders_before
    assert notifier.texts == [] and notifier2.texts == [], "healthy monitoring must not produce Telegram noise"


# =========================== stale / orphan / missing protection ===========================


def test_stale_bot_order_is_cancelled_but_the_customers_own_order_is_left_alone(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="spot")
    stale = client._new(client_id="mba" + "a" * 24 + "s9", side="sell", amount=0.001, trigger=10.0, conditional=True)
    mine = client._new(client_id="my-own-order", side="sell", amount=0.001, trigger=10.0, conditional=True)
    client.orders[stale["client_id"]] = stale
    client.orders[mine["client_id"]] = mine

    run_cycle(account_id, settings)

    assert client.orders[stale["client_id"]]["status"] == "canceled"
    assert client.orders["my-own-order"]["status"] == "open"
    assert client.orders_by_role("stop")[0]["status"] == "open", "the trade's own stop is never touched"


def test_stale_conditional_from_an_earlier_trade_is_cleared_before_a_new_entry(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    stale = client._new(
        client_id="mba" + "b" * 24 + "s1", side="sell", amount=0.0, trigger=10.0, conditional=True, close_position=True,
        kind="STOP_MARKET",
    )
    client.orders[stale["client_id"]] = stale

    run_cycle(account_id, settings)

    assert client.orders[stale["client_id"]]["status"] == "canceled"
    assert the_trade(account_id).status == "open"


def test_stop_removed_by_the_customer_is_replaced_and_the_trade_stays_protected(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="futures")
    first_stop = client.orders_by_role("stop")[0]
    first_stop["status"] = "canceled"  # customer cancels it on Binance

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    live = [o for o in client.orders_by_role("stop") if o["status"] == "open"]
    assert len(live) == 1 and live[0]["client_id"] != first_stop["client_id"]
    assert notifier.texts == [], "silently re-protected: no scan-spam, no false alarm"


def test_position_closed_outside_the_bot_cancels_the_leftover_stop_and_closes_the_trade(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="futures")
    client.externally_close_position()

    run_cycle(account_id, settings)

    assert the_trade(account_id).status == "closed"
    assert all(o["status"] != "open" for o in client.orders_by_role("stop")), "a closePosition stop must not outlive its position"


def test_an_existing_futures_position_the_bot_did_not_open_is_never_stacked_on_or_closed(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client()
    client.position = 1.0  # the customer's own position
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    run_cycle(account_id, settings, swallow=True)

    assert client.orders == {} and client.position == 1.0
    assert all_trades(account_id) == []
    with session_scope() as session:
        assert "already an open position" in session.get(TradingAccount, account_id).last_error


def test_an_unresolved_entry_with_a_position_on_the_exchange_goes_to_attention_not_void(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client()
    client.fail("create_order", role="entry", exc=ccxt.RequestTimeout("x"), when="before", times=1)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "pending"
    client.position = 0.2  # a position appears that the bot has no order for
    _age_trade(account_id, 600)

    notifier = run_cycle(account_id, settings)
    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "attention" and trade.attention_reason == "orphan_position"
    assert len([t for t in notifier.texts if t.startswith("ATTENTION")]) == 1


# =========================== emergency path that cannot finish ===========================


def test_emergency_close_that_cannot_be_verified_goes_to_attention_once_then_resolves(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client()
    client.fail("create_order", role="stop", exc=ccxt.InvalidOrder("no"), times=99)
    client.fail("create_order", role="exit", exc=ccxt.RequestTimeout("down"), times=99)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    notifier = run_cycle(account_id, settings)
    assert the_trade(account_id).status == "attention"
    run_reconcile(account_id, settings, notifier)
    run_reconcile(account_id, settings, notifier)
    assert the_trade(account_id).status == "attention"
    assert len([t for t in notifier.texts if t.startswith("ATTENTION")]) == 1, "one alert per episode, not per retry"
    assert client.position > 0, "the position really is still unprotected here"

    client.faults.clear()  # connectivity returns
    client.fail("create_order", role="stop", exc=ccxt.InvalidOrder("no"), times=99)
    run_reconcile(account_id, settings, notifier)

    trade = the_trade(account_id)
    assert trade.status == "closed" and client.position == 0.0 and client.violations == []
    assert any(t.startswith("Resolved") for t in notifier.texts)


# =========================== double scheduler execution ===========================


def test_two_scheduler_instances_cannot_both_open_a_trade_for_one_account(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings)

    async def attempt():
        with session_scope() as session:
            ctx = live_context(session, account_id, client, settings, entries_allowed=True)
            await ctx.venue.prepare()
            return await lifecycle.open_live_trade(
                ctx, side="long", quantity=0.2, signal_price=90.0, stop_price=43.0, risk_text=""
            )

    async def both():
        return await asyncio.gather(attempt(), attempt())

    results = asyncio.run(both())

    assert sorted(r is None for r in results) == [False, True], "exactly one winner"
    assert len(client.orders_by_role("entry")) == 1
    assert len([t for t in all_trades(account_id) if t.status != "void"]) == 1


def test_two_workers_claiming_the_same_exit_only_one_proceeds(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="futures")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE

    async def claim():
        with session_scope() as session:
            ctx = live_context(session, account_id, client, settings)
            await ctx.venue.prepare()
            trade = session.query(Trade).filter(Trade.account_id == account_id).one()
            return await lifecycle.execute_exit(ctx, trade, reason="crossover_exit")

    async def both():
        return await asyncio.gather(claim(), claim())

    results = asyncio.run(both())

    assert "closed" in results and len(client.orders_by_role("exit")) == 1
    assert client.violations == []


# =========================== software backstops ===========================


def test_a_market_stop_that_the_exchange_failed_to_fire_is_backstopped_by_the_bot(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="spot")
    client.price = 30.0  # market far below the 43.0 stop, yet Binance never fired it (set directly, no trigger)
    client._ohlcv = [[i, 30.0, 30.5, 29.5, 30.0, 1.0] for i in range(20)]

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "stop_loss"
    assert client.orders_by_role("stop")[0]["status"] == "canceled"
    assert len(client.orders_by_role("exit")) == 1 and trade.exit_price == pytest.approx(30.0)


# =========================== partial stop execution ===========================


def _spot_limit_client():
    return spot_client(order_types=["LIMIT", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"])


def test_spot_stop_limit_is_used_when_no_market_stop_is_listed_and_a_gap_through_is_handled(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = _spot_limit_client()
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.protective_kind == "stop_loss_limit"
    assert trade.protective_limit_price == pytest.approx(43.0 * 0.995, abs=0.011)

    client.move_price(30.0)  # gaps through both the trigger and the limit: the stop-limit never fills
    stop = client.orders_by_role("stop")[0]
    assert stop["status"] == "open" and stop.get("triggered")
    client._ohlcv = [[i, 30.0, 30.5, 29.5, 30.0, 1.0] for i in range(20)]

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "stop_limit_gap"
    assert stop["status"] == "canceled"
    assert len(client.orders_by_role("exit")) == 1 and trade.exit_price == pytest.approx(30.0)


def test_partially_executed_spot_stop_closes_only_the_remainder(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = _spot_limit_client()
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    stop = client.orders_by_role("stop")[0]
    half = round(stop["amount"] / 2, 3)

    # Triggered stop-limit fills half, then is cancelled (e.g. by the customer).
    client.price = 40.0
    client.free["BTC"] += client.used["BTC"] - half  # cancel releases the unfilled remainder
    client.used["BTC"] = 0.0
    client.free["USDT"] += half * 40.0
    client._add_fill("sell", half, 40.0)
    stop.update(filled=half, average=40.0, status="canceled")

    run_cycle(account_id, settings)

    done = the_trade(account_id)
    assert done.status == "closed"
    exits = client.orders_by_role("exit")
    assert len(exits) == 1 and exits[0]["amount"] <= trade.quantity - half + client.step
    assert done.exit_price == pytest.approx(40.0, abs=0.01)
    assert client.free.get("BTC", 0.0) < client.step * 2 and client.used.get("BTC", 0.0) < 1e-9


# =========================== idempotent exit retries ===========================


def test_exit_that_keeps_failing_is_surfaced_as_attention_not_retried_forever_silently(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="spot")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    client.fail("create_order", role="exit", exc=ccxt.RequestTimeout("down"), times=99)

    notifier = run_cycle(account_id, settings)
    assert the_trade(account_id).status == "closing"
    run_cycle(account_id, settings, notifier)

    trade = the_trade(account_id)
    assert trade.status == "attention" and trade.attention_reason == "exit_failed"
    assert len([t for t in notifier.texts if t.startswith("ATTENTION")]) == 1


# =========================== legacy rows, outages, credentials, secrecy ===========================


def _legacy_live_open_trade(account_id, *, market):
    with session_scope() as session:
        session.add(
            Trade(
                account_id=account_id, side="long", is_paper=False, entry_price=90.0, quantity=0.2, stop_loss_price=43.0,
                status="open", opened_at=datetime.now(timezone.utc),
            )
        )


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_a_pre_existing_unprotected_live_trade_is_protected_silently_on_the_next_pass(db, monkeypatch, market):
    settings = make_settings(max_notional_multiplier=100.0)
    client = (spot_client if market == "spot" else futures_client)(ohlcv=FLAT_OHLCV, price=100.0)
    if market == "spot":
        client.free["BTC"] = 0.2
    else:
        client.position = 0.2
    install(monkeypatch, client)
    account_id = new_account(settings, market_type=market)
    _legacy_live_open_trade(account_id, market=market)

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected" and trade.protection_attempt == 1
    assert len([o for o in client.orders_by_role("stop") if o["status"] == "open"]) == 1
    assert client.orders_by_role("exit") == [] and client.orders_by_role("entry") == []
    assert [t for t in notifier.texts if "is now live" not in t] == [], "re-protecting a legacy row is silent"


def test_exchange_outage_during_reconciliation_changes_nothing_and_sends_no_order(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="futures")
    orders = {k: dict(v) for k, v in client.orders.items()}
    real = client.fetch_positions

    async def down(symbols=None):
        raise ccxt.NetworkError("simulated outage")

    client.fetch_positions = down

    run_cycle(account_id, settings, swallow=True)

    assert the_trade(account_id).status == "open"
    assert {k: v["status"] for k, v in client.orders.items()} == {k: v["status"] for k, v in orders.items()}
    with session_scope() as session:
        assert "connection issue" in session.query(TradingAccount).one().last_error
    client.fetch_positions = real
    run_cycle(account_id, settings)  # outage over: healthy again
    with session_scope() as session:
        assert session.query(TradingAccount).one().last_error is None


def test_rejected_credentials_while_a_trade_is_open_change_nothing(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="spot")

    async def rejected(*a, **k):
        raise ccxt.AuthenticationError("-2015 Invalid API-key, IP, or permissions for action")

    client.fetch_order = rejected
    before = {k: v["status"] for k, v in client.orders.items()}

    run_cycle(account_id, settings, swallow=True)

    assert the_trade(account_id).status == "open"
    assert {k: v["status"] for k, v in client.orders.items()} == before
    with session_scope() as session:
        assert "rejected this account's API key" in session.query(TradingAccount).one().last_error


def test_repeating_the_protection_step_never_creates_a_second_stop(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="spot")

    async def again():
        with session_scope() as session:
            ctx = live_context(session, account_id, client, settings)
            await ctx.venue.prepare()
            trade = session.query(Trade).filter(Trade.account_id == account_id).one()
            for _ in range(3):
                assert await lifecycle._place_and_verify(ctx, trade) == "protected"

    asyncio.run(again())
    assert len(client.orders_by_role("stop")) == 1


def test_placing_an_entry_twice_with_one_client_id_is_refused_by_the_exchange_model(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="spot")
    trade = the_trade(account_id)

    async def dup():
        with session_scope() as session:
            ctx = live_context(session, account_id, client, settings, entries_allowed=True)
            await ctx.venue.prepare()
            await ctx.venue.place_entry("long", 0.1, lifecycle.client_id(trade, "entry"))

    with pytest.raises(ccxt.InvalidOrder):
        asyncio.run(dup())
    assert len(client.orders_by_role("entry")) == 1


def test_client_ids_are_deterministic_short_and_exchange_safe(db):
    import re

    t = Trade(id="12345678-1234-1234-1234-123456789abc")
    ids = {lifecycle.client_id(t, "entry"), lifecycle.client_id(t, "stop", 1), lifecycle.client_id(t, "stop", 2), lifecycle.client_id(t, "exit", 1)}
    assert len(ids) == 4
    assert all(re.fullmatch(r"[A-Za-z0-9]{1,36}", i) and i.startswith("mba") for i in ids)
    assert lifecycle.client_id(t, "stop", 1) == lifecycle.client_id(t, "stop", 1)


def test_no_exchange_error_text_or_credential_ever_reaches_customers_or_logs(db, monkeypatch, caplog):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client()
    leak = "LEAK-api-key-ABC123-secret-XYZ789 raw body {\"code\":-2015}"
    client.fail("create_order", role="stop", exc=ccxt.InvalidOrder(leak), times=99)
    client.fail("create_order", role="exit", exc=ccxt.RequestTimeout(leak), times=99)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", email="secrecy@example.com")

    import logging

    with caplog.at_level(logging.DEBUG):
        notifier = run_cycle(account_id, settings, swallow=True)
        run_reconcile(account_id, settings, notifier)

    haystack = "\n".join(notifier.texts) + "\n" + caplog.text
    with session_scope() as session:
        haystack += str(session.query(TradingAccount).one().last_error)
        trade = session.query(Trade).one()
        haystack += str(trade.attention_reason) + str(trade.close_reason)
    for needle in ("LEAK", "ABC123", "XYZ789", "-2015", "synthetic-secrecy@example.com-key", "synthetic-secrecy@example.com-secret"):
        assert needle not in haystack, needle
