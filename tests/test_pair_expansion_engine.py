"""Futures pair expansion: the trading engine side.

Scope being pinned: six extra USD-M Futures pairs (SOL, DOGE, SUI, LINK, ADA, LTC)
are selectable MANUALLY and only while `futures_pair_expansion` is on; Spot is
untouched; Auto Select's universe stays the original four; a market the
customer's capital cannot reach is skipped by Auto (live Futures, expansion on)
and never offered to be chosen. With the flag off nothing changes.

Everything exchange-side is the stateful FakeBinance (tests/fake_binance.py)."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import itertools

import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot import exchange, pair_catalog, scheduler, trader
from mba_bot.config import ALL_KNOWN_PAIRS, FUTURES_EXTRA_PAIRS, SUPPORTED_PAIRS, auto_pairs, manual_pairs
from mba_bot.db import init_db, session_scope
from mba_bot.models import TradingAccount

EXTRA = list(FUTURES_EXTRA_PAIRS)
ALL10 = list(ALL_KNOWN_PAIRS)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    yield
    scheduler._account_locks.clear()
    scheduler._stragglers.clear()


def _multi_market(client, min_notional_by_pair):
    """Make the fake publish a market for every pair in the dict, each with its own minimum notional
    (None = no notional filter at all)."""
    original = client.load_markets

    async def load_markets():
        await original()
        base = client.markets[client.symbol]
        markets = {}
        for pair, minimum in min_notional_by_pair.items():
            symbol = f"{pair}:USDT"
            market = copy.deepcopy(base)
            market["symbol"], market["id"] = symbol, pair.replace("/", "")
            kept = [f for f in market["info"]["filters"] if f["filterType"] not in ("NOTIONAL", "MIN_NOTIONAL")]
            if minimum is not None:
                kept.append({"filterType": "NOTIONAL", "minNotional": str(minimum)})
            market["info"]["filters"] = kept
            markets[symbol] = market
        client.markets = markets
        return markets

    client.load_markets = load_markets
    return client


def _fut(pair="BTC/USDT", **kw):
    """A Futures fake where the account has a (1x) leverage setting for THIS symbol, as a real account does.
    (The bot fails closed when it cannot read the leverage, which is itself asserted elsewhere.)"""
    kw.setdefault("leverage_by_symbol", {p: 1.0 for p in ALL10})
    return futures_client(pair=pair, **kw)


def _asked(client):
    return [c[1].split(":")[0] for c in client.calls if c[0] == "fetch_ohlcv"]


# ------------------------------------------------------------------ the universes


def test_universes_spot_and_auto_never_change_and_futures_extends_only_manually():
    for expansion in (False, True):
        assert manual_pairs("spot", futures_expansion=expansion) == SUPPORTED_PAIRS
        assert auto_pairs("spot") == auto_pairs("futures") == SUPPORTED_PAIRS
    assert manual_pairs("futures", futures_expansion=False) == SUPPORTED_PAIRS
    assert manual_pairs("futures", futures_expansion=True) == SUPPORTED_PAIRS + FUTURES_EXTRA_PAIRS
    assert len(ALL10) == 10 and set(EXTRA).isdisjoint(SUPPORTED_PAIRS)
    assert set(ALL10) == {"BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT", "DOGE/USDT", "SUI/USDT", "LINK/USDT", "ADA/USDT", "LTC/USDT"}


@pytest.mark.parametrize("expansion", [False, True])
def test_auto_select_only_ever_scans_the_original_four(db, monkeypatch, expansion):
    settings = make_settings(futures_pair_expansion=expansion)
    client = _fut()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        account.pair_selection_mode = "auto"
        account.bot_started_at = None  # paper

    async def go():
        with session_scope(autoflush=False) as session:
            account = session.get(TradingAccount, account_id)
            await trader._scan_auto_pair(client, account, settings)

    asyncio.run(go())

    assert sorted(_asked(client)) == sorted(SUPPORTED_PAIRS)
    assert not set(_asked(client)) & set(EXTRA)


# ------------------------------------------------------------------ Auto skips capital-unreachable markets


_scan_counter = itertools.count()


def _scan(client, settings, *, capital, market="futures"):
    account_id = new_account(settings, email=f"scan{next(_scan_counter)}@example.com", market_type=market)

    async def go():
        with session_scope(autoflush=False) as session:
            account = session.get(TradingAccount, account_id)
            return await trader._scan_auto_pair(client, account, settings, capital=capital)

    return asyncio.run(go())


def test_auto_skips_a_pair_whose_minimum_the_capital_cannot_reach_and_takes_the_next(db):
    settings = make_settings(futures_pair_expansion=True)  # multiplier 3
    client = _multi_market(_fut(), {"BTC/USDT": 100.0, "ETH/USDT": 20.0, "BNB/USDT": 5.0, "XRP/USDT": 5.0})
    # Every candidate has the SAME bullish signal, so BTC wins the tie by universe order unless skipped.

    unaffected = _scan(client, settings, capital=None)
    assert unaffected[0] == "BTC/USDT", "no capital known: exactly the original selection"

    client.calls.clear()
    skipped = _scan(client, settings, capital=20.0)  # 20 * 3 = 60 < 100
    assert skipped[0] == "ETH/USDT"
    assert "BTC/USDT" not in _asked(client), "an unreachable market is skipped before any data is fetched for it"

    enough = _scan(client, settings, capital=50.0)  # 150 >= 100
    assert enough[0] == "BTC/USDT", "reachable at the bot's own ceiling: not skipped"


def test_a_pair_with_an_unknown_minimum_is_never_skipped(db):
    settings = make_settings(futures_pair_expansion=True)
    client = _multi_market(_fut(), {"BTC/USDT": None, "ETH/USDT": 5.0, "BNB/USDT": 5.0, "XRP/USDT": 5.0})
    assert _scan(client, settings, capital=1.0)[0] == "BTC/USDT"


def test_if_the_markets_cannot_be_read_auto_scans_exactly_as_before(db):
    settings = make_settings(futures_pair_expansion=True)
    client = _fut()

    async def broken():
        raise ccxt_error()

    def ccxt_error():
        import ccxt.async_support as ccxt

        return ccxt.NetworkError("synthetic")

    client.load_markets = broken
    result = _scan(client, settings, capital=1.0)
    assert result is None or result[0] in SUPPORTED_PAIRS  # no crash; behaviour as without a capital


def test_a_live_futures_auto_account_with_the_expansion_on_trades_the_next_pair_not_the_unreachable_one(db, monkeypatch):
    settings = make_settings(futures_pair_expansion=True, max_notional_multiplier=3.0)
    # BTC's minimum (1000) is out of reach of 300 * 3 = 900; the others are small enough that the
    # risk-based size still clears the exchange's minimum.
    client = _multi_market(_fut("ETH/USDT"), {"BTC/USDT": 1000.0, "ETH/USDT": 0.5, "BNB/USDT": 0.5, "XRP/USDT": 0.5})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", allocated_capital=300.0)
    with session_scope() as session:
        session.get(TradingAccount, account_id).pair_selection_mode = "auto"

    run_cycle(account_id, settings)

    with session_scope() as session:
        assert session.get(TradingAccount, account_id).pair == "ETH/USDT"
    trade = the_trade(account_id)
    assert trade.pair == "ETH/USDT" and trade.status == "open" and trade.protection_status == "protected"
    assert "BTC/USDT" not in _asked(client)


def test_a_paper_futures_auto_account_is_never_capital_filtered_even_with_the_expansion_on(db, monkeypatch):
    settings = make_settings(futures_pair_expansion=True, demo_futures_api_key="k", demo_futures_api_secret="s")
    client = _multi_market(_fut(), {"BTC/USDT": 100.0, "ETH/USDT": 20.0, "BNB/USDT": 5.0, "XRP/USDT": 5.0})
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", allocated_capital=1.0)
    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        account.pair_selection_mode = "auto"
        account.bot_started_at = None  # paper

    run_cycle(account_id, settings)

    with session_scope() as session:
        assert session.get(TradingAccount, account_id).pair == "BTC/USDT", "paper Auto is exactly as before"


def test_spot_auto_is_never_capital_filtered(db, monkeypatch):
    settings = make_settings(futures_pair_expansion=True)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="spot", allocated_capital=1.0)
    with session_scope() as session:
        session.get(TradingAccount, account_id).pair_selection_mode = "auto"
    run_cycle(account_id, settings)
    assert sorted(set(_asked(client))) == sorted(SUPPORTED_PAIRS), "all four scanned: no capital skip on Spot"


# ------------------------------------------------------------------ manual trading on the new pairs


@pytest.mark.parametrize("pair", EXTRA)
def test_each_extra_pair_opens_a_protected_long_with_a_fail_safe_stop(db, monkeypatch, pair):
    settings = make_settings(futures_pair_expansion=True, max_notional_multiplier=100.0)
    client = _fut(pair)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", pair=pair)

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.pair == pair and trade.side == "long"
    assert trade.status == "open" and trade.protection_status == "protected"
    assert len(client.orders_by_role("entry")) == 1 and len([o for o in client.orders_by_role("stop") if o["status"] == "open"]) == 1
    assert any(pair in t and "Entered" in t or pair in t for t in notifier.texts), "the customer is told which pair"


@pytest.mark.parametrize("pair", EXTRA)
def test_each_extra_pair_opens_a_protected_short_and_exits_reduce_only(db, monkeypatch, pair):
    settings = make_settings(futures_pair_expansion=True, max_notional_multiplier=100.0)
    client = _fut(pair, ohlcv=BEARISH_OHLCV, price=BEAR_PRICE)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", pair=pair)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.side == "short" and trade.pair == pair and trade.protection_status == "protected"
    client._ohlcv, client.price = BULLISH_OHLCV, ENTRY_PRICE  # the opposite crossover
    notifier = run_cycle(account_id, settings)
    assert the_trade(account_id).status == "closed" and the_trade(account_id).close_reason == "crossover_exit"
    assert client.position == 0.0, "flat after the exit"
    assert any(pair in t for t in notifier.texts)


@pytest.mark.parametrize("pair", ALL10)
def test_risk_and_capital_invariants_hold_on_every_one_of_the_ten_pairs(db, monkeypatch, pair):
    settings = make_settings(futures_pair_expansion=True, max_notional_multiplier=3.0)
    client = _fut(pair)
    install(monkeypatch, client)
    capital = 1000.0
    account_id = new_account(settings, market_type="futures", pair=pair, allocated_capital=capital)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    if trade is None:
        return  # sized below the exchange's minimums: nothing is ever forced
    risk = trade.quantity * abs(trade.entry_price - trade.stop_loss_price)
    assert risk <= capital * settings.risk_pct_default / 100 * (1 + 1e-6), "never more than the configured risk percentage"
    assert trade.quantity * trade.entry_price <= capital * settings.max_notional_multiplier * (1 + 1e-6), "never past the notional ceiling"
    assert len(all_trades(account_id)) == 1


def test_a_second_cycle_never_creates_a_duplicate_position_on_an_extra_pair(db, monkeypatch):
    settings = make_settings(futures_pair_expansion=True, max_notional_multiplier=100.0)
    client = _fut("SOL/USDT")
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", pair="SOL/USDT")
    for _ in range(3):
        run_cycle(account_id, settings)
    assert len(all_trades(account_id)) == 1 and len(client.orders_by_role("entry")) == 1


# ------------------------------------------------------------------ the entry guard and existing positions


def test_with_the_expansion_off_a_new_entry_on_an_extra_pair_is_refused_and_explained(db, monkeypatch):
    settings = make_settings(futures_pair_expansion=False, max_notional_multiplier=100.0)
    client = _fut("DOGE/USDT")
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", pair="DOGE/USDT")

    notifier = run_cycle(account_id, settings)

    assert client.orders == {} and all_trades(account_id) == []
    with session_scope() as session:
        assert "not currently available" in session.get(TradingAccount, account_id).last_error
    assert any("not currently available" in t for t in notifier.texts)


def test_an_open_position_on_an_extra_pair_is_still_managed_after_the_expansion_is_switched_off(db, monkeypatch):
    on = make_settings(futures_pair_expansion=True, max_notional_multiplier=100.0)
    client = _fut("LINK/USDT")
    install(monkeypatch, client)
    account_id = new_account(on, market_type="futures", pair="LINK/USDT")
    run_cycle(account_id, on)
    assert the_trade(account_id).protection_status == "protected"
    stop = [o for o in client.orders_by_role("stop") if o["status"] == "open"][0]
    stop["status"] = "canceled"  # the customer removed the stop

    off = dataclasses.replace(on, futures_pair_expansion=False)  # same encryption key, flag switched off
    run_cycle(account_id, off)  # poll cycle
    stop2 = [o for o in client.orders_by_role("stop") if o["status"] == "open"]
    assert len(stop2) == 1 and the_trade(account_id).protection_status == "protected", "managed through the trade's own pair"
    assert len(client.orders_by_role("entry")) == 1, "and no second entry"

    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE  # strategy exit still works with the flag off
    run_cycle(account_id, off)
    assert the_trade(account_id).status == "closed"


def test_an_open_position_always_takes_priority_over_any_other_pair_signalling(db, monkeypatch):
    settings = make_settings(futures_pair_expansion=True, max_notional_multiplier=100.0)
    client = _fut("ADA/USDT")
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", pair="ADA/USDT")
    run_cycle(account_id, settings)
    client.calls.clear()
    with session_scope() as session:
        session.get(TradingAccount, account_id).pair_selection_mode = "auto"  # even if switched to Auto mid-trade
    run_cycle(account_id, settings)
    assert set(_asked(client)) <= {"ADA/USDT"}, "no scan of any other pair while a position is open"
    assert len(all_trades(account_id)) == 1


# ------------------------------------------------------------------ several accounts, recovery, Telegram


def test_several_accounts_on_different_extra_pairs_each_hold_exactly_one_position(db, monkeypatch):
    settings = make_settings(futures_pair_expansion=True, max_notional_multiplier=100.0)
    clients = {f"{p.split('/')[0].lower()}@example.com": _fut(p) for p in EXTRA}
    install(monkeypatch, next(iter(clients.values())))
    keys = {f"synthetic-{e}-key": c for e, c in clients.items()}
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kw: keys[kw["api_key"]])
    notifier = FakeNotifier()
    monkeypatch.setattr(scheduler, "build_notifier", lambda *a, **k: notifier)
    ids = {e: new_account(settings, email=e, market_type="futures", pair=p) for (e, p) in zip(clients, EXTRA)}

    asyncio.run(scheduler.run_once(settings))
    asyncio.run(scheduler.run_once(settings))

    for (email, account_id), pair in zip(ids.items(), EXTRA):
        trades = all_trades(account_id)
        assert len(trades) == 1 and trades[0].pair == pair and trades[0].protection_status == "protected", email
        assert len(clients[email].orders_by_role("entry")) == 1


def test_restart_recovery_and_reconciliation_work_for_an_extra_pair(db, monkeypatch):
    settings = make_settings(futures_pair_expansion=True, max_notional_multiplier=100.0)
    client = _fut("SUI/USDT")
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures", pair="SUI/USDT")
    run_cycle(account_id, settings)
    orders = len(client.orders)

    asyncio.run(scheduler.recover_once(settings, include_healthy=True))  # a restart's startup reconciliation
    assert len(client.orders) == orders and the_trade(account_id).status == "open", "a healthy trade is left alone"

    stop = [o for o in client.orders_by_role("stop") if o["status"] == "open"][0]
    stop["status"] = "canceled"
    asyncio.run(scheduler.recover_once(settings, include_healthy=True))
    assert len([o for o in client.orders_by_role("stop") if o["status"] == "open"]) == 1
    assert the_trade(account_id).pair == "SUI/USDT"
