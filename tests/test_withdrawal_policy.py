"""Withdrawal-permission policy.

ENTRY-CAPABLE credential validation vs RISK-REDUCING management of an
already-open position:

* A key that can withdraw never reaches an entry order (Spot or Futures),
  and the block is durable: it survives restarts, survives the permission
  later being removed on the same key, and is lifted only by replacing the
  credential (not possible while a trade is non-terminal).
* An already-open position keeps being managed with such a key - reconcile,
  verify/re-place the bot's own protective order, the exit lease, emergency
  close - on a venue that CANNOT place an entry. It is not liquidated merely
  because the permission exists.
* The customer gets one clear alert.

Exchange behaviour comes from tests/fake_binance.py (assumptions, not Binance facts)."""
from __future__ import annotations

import asyncio

import ccxt.async_support as ccxt
import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot import exchange, lifecycle, scheduler, venue as venue_mod
from mba_bot.db import init_db, session_scope
from mba_bot.models import TradingAccount

MESSAGE = (
    "Your Binance API key currently has withdrawal permission enabled. No new trades will be opened. "
    "Any existing position will continue to be managed for risk reduction only. Replace the API key with one "
    "that has withdrawal permission disabled before new trading can resume."
)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _alerts(notifier):
    return [t for t in notifier.texts if "withdrawal permission" in t]


def _blocked_at(account_id):
    with session_scope() as session:
        return session.get(TradingAccount, account_id).withdrawal_blocked_at


# =========================== A. no open position: nothing can enter ===========================


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_a_withdrawal_capable_key_can_never_open_a_new_trade(db, monkeypatch, market):
    settings = make_settings(max_notional_multiplier=100.0)
    client = (spot_client if market == "spot" else futures_client)(can_withdraw=True)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type=market)

    notifier = run_cycle(account_id, settings)
    run_cycle(account_id, settings, notifier)

    assert client.orders == {} and all_trades(account_id) == []
    assert getattr(client, "position", 0.0) == 0.0
    assert _blocked_at(account_id) is not None
    assert _alerts(notifier) == [MESSAGE], "exactly one alert, exactly the approved wording"


def test_the_alert_is_exact_and_leaks_nothing(db, monkeypatch):
    settings = make_settings()
    client = spot_client(can_withdraw=True)
    install(monkeypatch, client)
    account_id = new_account(settings, email="leaky@example.com")
    notifier = run_cycle(account_id, settings)
    assert notifier.texts.count(MESSAGE) == 1
    assert exchange.WITHDRAWAL_BLOCK_MESSAGE == MESSAGE
    for needle in ("synthetic", "canWithdraw", "-2015", "secret"):
        assert needle not in MESSAGE


def test_the_block_persists_across_restarts_and_the_alert_never_repeats(db, monkeypatch, tmp_path):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(can_withdraw=True)
    install(monkeypatch, client)
    account_id = new_account(settings)
    notifier = run_cycle(account_id, settings)

    # "restart": a fresh engine/session factory over the same database file
    init_db(f"sqlite:///{tmp_path}/test.db")
    run_cycle(account_id, settings, notifier)
    asyncio.run(scheduler.recover_once(settings, include_healthy=True))
    run_reconcile(account_id, settings, notifier)

    assert _blocked_at(account_id) is not None
    assert len(_alerts(notifier)) == 1 and client.orders == {}


def test_permission_removed_on_the_same_key_does_not_lift_the_block(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(can_withdraw=True)
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)

    client.can_withdraw = False  # edited on Binance; same stored credential
    run_cycle(account_id, settings)

    assert client.orders == {} and _blocked_at(account_id) is not None, "only a REPLACED credential resumes trading"


def test_after_replacement_a_safe_key_trades_and_a_still_unsafe_key_is_blocked_again_with_a_new_alert(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(can_withdraw=True)
    install(monkeypatch, client)
    account_id = new_account(settings)
    notifier = run_cycle(account_id, settings)

    def replace():  # what PUT /api/account/credentials does on success
        with session_scope() as session:
            session.get(TradingAccount, account_id).withdrawal_blocked_at = None

    replace()  # new key, but it can still withdraw
    run_cycle(account_id, settings, notifier)
    assert client.orders == {} and len(_alerts(notifier)) == 2, "a new episode is a new (single) alert"

    replace()
    client.can_withdraw = False  # genuinely safe key
    run_cycle(account_id, settings, notifier)
    assert the_trade(account_id).status == "open"


def test_a_paper_account_is_not_subject_to_the_live_key_check(db, monkeypatch):
    settings = make_settings()
    client = spot_client(can_withdraw=True)
    install(monkeypatch, client)
    account_id = new_account(settings)
    with session_scope() as session:
        a = session.get(TradingAccount, account_id)
        a.bot_started_at = None  # resolves to PAPER: testnet key, no live check
    run_cycle(account_id, settings)
    assert _blocked_at(account_id) is None


# =========================== E. code-level separation ===========================


def test_venues_are_risk_reducing_only_unless_the_entry_path_says_otherwise():
    for market in ("spot", "futures"):
        v = venue_mod.build_venue(object(), market_type=market, pair="BTC/USDT")
        assert v.entries_allowed is False
        with pytest.raises(venue_mod.EntryForbidden):
            asyncio.run(v.place_entry("long", 0.1, "mbaX"))


def test_open_live_trade_refuses_a_risk_reducing_venue(db, monkeypatch):
    settings = make_settings()
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings)

    async def go():
        with session_scope() as session:
            ctx = live_context(session, account_id, client, settings)  # default: risk-reducing only
            await ctx.venue.prepare()
            await lifecycle.open_live_trade(ctx, side="long", quantity=0.1, signal_price=90.0, stop_price=43.0, risk_text="")

    with pytest.raises(venue_mod.EntryForbidden):
        asyncio.run(go())
    assert client.orders == {} and all_trades(account_id) == []


def test_the_entry_path_validates_the_credential_itself_even_if_called_directly(db, monkeypatch):
    """_maybe_enter re-validates the key on every entry, independent of the
    cycle-level check: calling it with a withdrawal-capable key places nothing."""
    from mba_bot import trader
    from mba_bot.strategy import SignalType, generate_signal, candles_from_ohlcv

    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(can_withdraw=True)
    install(monkeypatch, client)
    account_id = new_account(settings)
    signal = generate_signal(candles_from_ohlcv(BULLISH_OHLCV), **STRATEGY_KWARGS)
    assert signal.signal == SignalType.BULLISH_CROSSOVER

    async def go():
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            await trader._maybe_enter(
                session, account, client, signal, ENTRY_PRICE, settings, FakeNotifier(), is_paper=False,
                now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )

    asyncio.run(go())
    assert client.orders == {} and all_trades(account_id) == []
    assert _blocked_at(account_id) is not None


# =========================== B. existing position: risk-reducing management continues ===========================


def _cancel_stop_like_binance(client):
    """The customer cancels the resting stop on Binance: the order ends and, on
    Spot, the reserved base is released."""
    stop = [o for o in client.orders_by_role("stop") if o["status"] == "open"][0]
    stop["status"] = "canceled"
    if client.market_type == "spot":
        client._unlock_base(stop["amount"] - stop["filled"])
    return stop


def _open_then_key_becomes_withdrawal_capable(monkeypatch, market):
    client, settings, account_id = open_protected(monkeypatch, market=market)
    client.can_withdraw = True
    return client, settings, account_id


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_existing_protection_is_still_reconciled_and_re_established(db, monkeypatch, market):
    client, settings, account_id = _open_then_key_becomes_withdrawal_capable(monkeypatch, market)
    first = _cancel_stop_like_binance(client)  # the stop disappears

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    live = [o for o in client.orders_by_role("stop") if o["status"] == "open"]
    assert len(live) == 1 and live[0]["client_id"] != first["client_id"]
    assert client.orders_by_role("entry")[0] is not None and len(client.orders_by_role("entry")) == 1, "no second entry"
    assert _alerts(notifier) == [MESSAGE], "one alert, and the position is NOT liquidated"
    assert trade.close_reason is None


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_a_protected_position_is_left_alone_not_liquidated_because_of_the_permission(db, monkeypatch, market):
    client, settings, account_id = _open_then_key_becomes_withdrawal_capable(monkeypatch, market)
    orders = len(client.orders)
    run_cycle(account_id, settings)
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open" and len(client.orders) == orders
    assert client.orders_by_role("exit") == []


def test_the_recovery_loop_also_manages_an_existing_position_with_such_a_key(db, monkeypatch):
    client, settings, account_id = _open_then_key_becomes_withdrawal_capable(monkeypatch, "futures")
    _cancel_stop_like_binance(client)
    notifier = run_reconcile(account_id, settings)
    assert the_trade(account_id).protection_status == "protected" and len(_alerts(notifier)) == 1


def test_spot_risk_reducing_exit_works_and_sells_only_the_bots_quantity(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="spot")
    client.free["BTC"] += 5.0  # the customer's own units of the same asset
    client.can_withdraw = True
    held = the_trade(account_id).quantity
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "crossover_exit"
    assert client.orders_by_role("exit")[0]["amount"] <= held + 1e-12
    assert client.free["BTC"] >= 5.0 - 1e-9
    assert len(_alerts(notifier)) == 1


def test_futures_reduce_only_exit_works_and_cannot_reverse_or_increase_exposure(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="futures")
    client.can_withdraw = True
    qty = the_trade(account_id).quantity
    client.position = qty * 2  # more than the bot opened
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE

    run_cycle(account_id, settings)

    assert the_trade(account_id).status == "closed"
    exit_order = client.orders_by_role("exit")[0]
    assert exit_order["reduce_only"] and exit_order["amount"] <= qty + 1e-9
    assert client.position > 0 and client.violations == []

    # a repeated exit on a flat account can neither open nor reverse anything
    client.position = 0.0
    run_cycle(account_id, settings)
    assert client.position == 0.0 and client.violations == []


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_emergency_close_still_works_when_protection_cannot_be_re_established(db, monkeypatch, market):
    client, settings, account_id = _open_then_key_becomes_withdrawal_capable(monkeypatch, market)
    _cancel_stop_like_binance(client)
    client.fail("create_order", role="stop", exc=ccxt.InvalidOrder("rejected"), times=99)

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "protection_failed_emergency_exit"
    assert len(client.orders_by_role("entry")) == 1 and len(client.orders_by_role("exit")) == 1
    if market == "futures":
        assert client.position == 0.0 and client.violations == []
    assert len(_alerts(notifier)) == 1


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_stop_trigger_reconciliation_continues(db, monkeypatch, market):
    client, settings, account_id = _open_then_key_becomes_withdrawal_capable(monkeypatch, market)
    client.move_price(40.0)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "exchange_stop_triggered"
    assert client.orders_by_role("exit") == []


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_after_the_trade_is_flat_a_new_entry_stays_blocked(db, monkeypatch, market):
    client, settings, account_id = _open_then_key_becomes_withdrawal_capable(monkeypatch, market)
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    notifier = run_cycle(account_id, settings)
    assert the_trade(account_id).status == "closed"

    client.can_withdraw = False  # even if the permission is later removed on the same key
    client._ohlcv, client.price = BULLISH_OHLCV, ENTRY_PRICE
    for _ in range(3):
        run_cycle(account_id, settings, notifier)

    assert len([t for t in all_trades(account_id) if t.status != "void"]) == 1, "no second trade"
    assert len(client.orders_by_role("entry")) == 1
    assert _blocked_at(account_id) is not None and len(_alerts(notifier)) == 1


def test_restart_during_an_open_position_keeps_managing_it_and_keeps_the_block(db, monkeypatch, tmp_path):
    client, settings, account_id = _open_then_key_becomes_withdrawal_capable(monkeypatch, "futures")
    notifier = run_cycle(account_id, settings)
    assert _blocked_at(account_id) is not None

    init_db(f"sqlite:///{tmp_path}/test.db")  # restart
    _cancel_stop_like_binance(client)
    asyncio.run(scheduler.recover_once(settings, include_healthy=True))
    run_cycle(account_id, settings, notifier)

    assert the_trade(account_id).status == "open" and the_trade(account_id).protection_status == "protected"
    assert len(_alerts(notifier)) == 1 and len(client.orders_by_role("entry")) == 1


# =========================== F. F-02: the API KEY's withdrawal permission, not the account-level flag ===========================
#
# Measured on a real Mainnet account (read-only): `account.canWithdraw` was True on both Spot and Futures while the
# key's own restriction `enableWithdrawals` was False. The gate must follow the KEY. FakeBinance now reports the
# account-level flag as True whatever the key says, and drives `enableWithdrawals` from `can_withdraw`.

UNVERIFIABLE = [
    pytest.param({"fault": ccxt.RequestTimeout("synthetic timeout")}, id="timeout"),
    pytest.param({"fault": ccxt.ExchangeNotAvailable("synthetic unavailable")}, id="exchange-unavailable"),
    pytest.param({"fault": ccxt.AuthenticationError("synthetic rejected")}, id="key-rejected"),
    pytest.param({"override": {}}, id="field-missing"),
    pytest.param({"override": {"enableWithdrawals": None}}, id="field-null"),
    pytest.param({"override": {"enableWithdrawals": "false"}}, id="field-not-a-boolean"),
    pytest.param({"override": ["unexpected"]}, id="response-not-an-object"),
]


def _make(market, **kw):
    return (spot_client if market == "spot" else futures_client)(**kw)


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_f02_account_level_can_withdraw_true_but_key_enable_withdrawals_false_trades_normally(db, monkeypatch, market):
    """THE real-world case: Binance says the account can withdraw, and the key cannot."""
    settings = make_settings(max_notional_multiplier=100.0)
    client = _make(market, can_withdraw=False)
    assert asyncio.run(client.fetch_balance())["info"]["canWithdraw"] is True, "the account-level flag is True"
    assert asyncio.run(client.sapiGetAccountApiRestrictions())["enableWithdrawals"] is False, "the key cannot withdraw"
    install(monkeypatch, client)
    account_id = new_account(settings, market_type=market)

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert _blocked_at(account_id) is None and _alerts(notifier) == []


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_f02_a_key_that_can_withdraw_stays_blocked_whatever_the_account_flag_says(db, monkeypatch, market):
    settings = make_settings(max_notional_multiplier=100.0)
    client = _make(market, can_withdraw=True)
    assert asyncio.run(client.fetch_balance())["info"]["canWithdraw"] is True
    install(monkeypatch, client)
    account_id = new_account(settings, market_type=market)

    notifier = run_cycle(account_id, settings)
    run_cycle(account_id, settings, notifier)

    assert client.orders == {} and all_trades(account_id) == []
    assert _blocked_at(account_id) is not None
    assert _alerts(notifier) == [MESSAGE]


def test_f02_the_gate_reads_only_the_key_level_endpoint():
    client = spot_client(can_withdraw=False)
    assert asyncio.run(exchange.has_withdrawal_permission(client)) is False
    asyncio.run(exchange.assert_trade_only(client))  # passes
    client.can_withdraw = True
    assert asyncio.run(exchange.has_withdrawal_permission(client)) is True
    with pytest.raises(exchange.WithdrawalPermissionDetected):
        asyncio.run(exchange.assert_trade_only(client))
    assert {call[0] for call in client.calls} == {"api_restrictions"}, "no balance/account-level read is used"


@pytest.mark.parametrize("market", ["spot", "futures"])
@pytest.mark.parametrize("mode", UNVERIFIABLE)
def test_f02_an_unverifiable_key_permission_opens_no_trade_and_never_sets_the_permanent_block(db, monkeypatch, market, mode):
    settings = make_settings(max_notional_multiplier=100.0)
    client = _make(market, can_withdraw=False)
    if "fault" in mode:
        client.fail("api_restrictions", exc=mode["fault"], times=99)
    else:
        client.restrictions_override = mode["override"]
    install(monkeypatch, client)
    account_id = new_account(settings, market_type=market)

    notifier = run_cycle(account_id, settings, swallow=True)
    run_cycle(account_id, settings, notifier, swallow=True)

    assert client.orders == {} and all_trades(account_id) == [], "fail closed: no new trade"
    assert _blocked_at(account_id) is None, "a failed READ is not a finding about the key: no permanent block"
    assert _alerts(notifier) == [], "and no (false) withdrawal alert"
    with session_scope() as session:
        assert session.get(TradingAccount, account_id).last_error, "the customer sees a safe retry message"

    client.faults.clear()  # Binance answers properly again
    client.restrictions_override = None
    run_cycle(account_id, settings, notifier)
    assert the_trade(account_id).status == "open", "the very next healthy cycle trades: nothing was latched"


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_f02_managing_an_open_position_continues_when_the_key_permission_cannot_be_read(db, monkeypatch, market):
    client, settings, account_id = open_protected(monkeypatch, market=market)
    first = _cancel_stop_like_binance(client)  # the customer removes the stop
    client.fail("api_restrictions", exc=ccxt.RequestTimeout("synthetic"), times=99)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    live = [o for o in client.orders_by_role("stop") if o["status"] == "open"]
    assert len(live) == 1 and live[0]["client_id"] != first["client_id"], "the stop was re-established"
    assert len(client.orders_by_role("entry")) == 1 and _blocked_at(account_id) is None


def test_f02_the_recovery_loop_also_keeps_managing_when_the_key_permission_cannot_be_read(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="futures")
    _cancel_stop_like_binance(client)
    client.restrictions_override = {}  # malformed answer
    run_reconcile(account_id, settings)
    assert the_trade(account_id).protection_status == "protected" and _blocked_at(account_id) is None


def test_f02_the_unverifiable_message_is_customer_safe():
    message = exchange.safe_error_message(exchange.WithdrawalPermissionUnverifiable("internal detail enableWithdrawals"))
    assert "retry automatically" in message
    for needle in ("enableWithdrawals", "canWithdraw", "internal", "secret", "-2015"):
        assert needle not in message
