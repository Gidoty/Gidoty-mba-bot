"""Account-mode / capability gates, symbol normalization, and Spot sizing
rules (net-of-fee quantity, steps, minimum notional)."""
from __future__ import annotations

import ccxt.async_support as ccxt
import pytest
from fake_binance import FakeBinance
from helpers_live import *  # noqa: F401,F403

from mba_bot import exchange, venue as venue_mod
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade, TradingAccount


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _last_error(account_id):
    with session_scope() as session:
        return session.get(TradingAccount, account_id).last_error


# =========================== unsupported Futures account modes (fail closed) ===========================


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        (dict(hedged=True), "Hedge Mode"),
        (dict(multi_assets=True), "Multi-Assets"),
        (dict(portfolio_margin=True), "Portfolio Margin"),
        (dict(position_mode_raises=True), "mode this bot does not support"),
    ],
)
def test_unsupported_futures_account_modes_place_no_entry(db, monkeypatch, kwargs, needle):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client(**kwargs)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    run_cycle(account_id, settings)

    assert client.orders == {} and client.position == 0.0
    assert all_trades(account_id) == []
    assert "does not support" in _last_error(account_id)


def test_unsupported_mode_on_an_existing_position_goes_to_attention_and_touches_nothing(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="futures")
    client.hedged = True  # customer flips the account to Hedge Mode mid-trade
    orders_before = len(client.orders)

    notifier = run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "attention" and trade.attention_reason == "cannot_verify_exchange"
    assert len(client.orders) == orders_before, "no order may be sent on an account in an unverifiable mode"
    assert len([t for t in notifier.texts if t.startswith("ATTENTION")]) == 1


def test_portfolio_margin_flag_in_ccxt_options_is_refused():
    import asyncio

    client = FakeBinance(market_type="futures")
    client.options["portfolioMargin"] = True
    with pytest.raises(exchange.UnsupportedAccountMode):
        asyncio.run(exchange.verify_futures_account_mode(client, client.symbol))


def test_spot_never_runs_the_futures_mode_checks(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(hedged=True, multi_assets=True, portfolio_margin=True)  # meaningless on Spot
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open"
    assert not any(c[0] in ("fetch_position_mode", "multi_assets") for c in client.calls)


# =========================== capability: nothing is assumed ===========================


def test_spot_symbol_with_no_protective_order_type_is_refused(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(order_types=["LIMIT", "MARKET"])
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    assert client.orders == {} and all_trades(account_id) == []
    assert "protective stop cannot currently be placed" in _last_error(account_id)


def test_futures_contract_without_stop_market_is_refused(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client(order_types=["LIMIT", "MARKET"])
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    run_cycle(account_id, settings)
    assert client.orders == {} and all_trades(account_id) == []


def test_market_stop_is_preferred_over_stop_limit_when_both_are_listed(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()  # lists STOP_LOSS and STOP_LOSS_LIMIT
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    assert the_trade(account_id).protective_kind == "stop_loss"


def test_missing_symbol_filters_refuse_the_entry_rather_than_guess(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    real_load = client.load_markets

    async def no_filters():
        markets = await real_load()
        markets[client.symbol]["info"]["filters"] = []
        return markets

    client.load_markets = no_filters
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    assert client.orders == {} and all_trades(account_id) == []


def test_a_spot_market_that_is_not_trading_is_refused(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    real_load = client.load_markets

    async def halted():
        markets = await real_load()
        markets[client.symbol]["info"]["status"] = "BREAK"
        return markets

    client.load_markets = halted
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    assert client.orders == {}


# =========================== symbol normalization ===========================


def test_symbol_normalization():
    assert exchange.exchange_symbol("BTC/USDT", "spot") == "BTC/USDT"
    assert exchange.exchange_symbol("BTC/USDT", "futures") == "BTC/USDT:USDT"
    assert exchange.exchange_symbol("ETH/USDT", "futures") == "ETH/USDT:USDT"


def test_the_futures_client_is_never_sent_a_bare_spot_symbol(db, monkeypatch):
    """FakeBinance asserts on a bare 'BTC/USDT' sent to a Futures client for
    OHLCV, and raises BadSymbol for orders - the exact failure ccxt's market()
    produces. A full futures cycle must therefore use ccxt symbols throughout."""
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    run_cycle(account_id, settings)
    symbols = {c[1] for c in client.calls if c[0] in ("fetch_ohlcv", "create_order", "fetch_leverage")}
    assert symbols == {"BTC/USDT:USDT"}


def test_verify_market_rejects_wrong_kind():
    import asyncio

    client = FakeBinance(market_type="spot")
    asyncio.run(client.load_markets())
    with pytest.raises(exchange.UnsupportedSymbol):
        exchange.verify_market(client, "ETH/USDT", market_type="spot")
    with pytest.raises(exchange.UnsupportedSymbol):
        exchange.verify_market(client, client.symbol, market_type="futures")


# =========================== Spot net quantity / steps / minimums ===========================


def _spot_venue(**kw):
    import asyncio

    client = FakeBinance(market_type="spot", **kw)
    v = venue_mod.SpotVenue(client, "BTC/USDT")
    asyncio.run(v.prepare())
    return v


def test_floor_to_step_never_rounds_up():
    assert venue_mod.floor_to_step(0.2129, 0.001) == pytest.approx(0.212)
    assert venue_mod.floor_to_step(0.0009, 0.001) == 0.0
    assert venue_mod.ceil_to_step(43.001, 0.01) == pytest.approx(43.01)


def test_spot_net_quantity_subtracts_the_base_commission_and_floors_to_the_step():
    v = _spot_venue()
    order = venue_mod.OrderInfo("1", "c", "closed", 0.212, 0.212, 90.0, fee_base=0.000212, fee_known=True)
    assert v.net_quantity(order) == pytest.approx(0.211)
    assert v.net_quantity(order) <= order.filled - order.fee_base


def test_spot_net_quantity_assumes_the_standard_fee_when_the_response_has_no_fee_info():
    v = _spot_venue()
    order = venue_mod.OrderInfo("1", "c", "closed", 0.212, 0.212, 90.0, fee_base=0.0, fee_known=False)
    assert v.net_quantity(order) <= 0.212 * 0.999


def test_spot_fee_paid_in_another_asset_leaves_the_full_fill_exitable():
    v = _spot_venue()
    order = venue_mod.OrderInfo("1", "c", "closed", 0.212, 0.212, 90.0, fee_base=0.0, fee_known=True)  # paid in BNB
    assert v.net_quantity(order) == pytest.approx(0.212)


def test_entry_quantity_is_floored_and_rejected_below_the_minimums():
    v = _spot_venue(min_notional=5.0)
    assert v.plan_entry_quantity(0.21299, entry_price=90.0, stop_price=43.0, side="long") == pytest.approx(0.212)
    assert v.plan_entry_quantity(0.0009, entry_price=90.0, stop_price=43.0, side="long") == 0.0  # below minQty
    assert v.plan_entry_quantity(0.05, entry_price=90.0, stop_price=43.0, side="long") == 0.0  # 4.5 < minNotional 5
    assert v.plan_entry_quantity(0.212, entry_price=90.0, stop_price=43.0, side="short") == 0.0  # Spot is long-only


def test_entry_is_refused_if_the_post_fee_quantity_could_not_be_protected():
    # 0.1 * 90 = 9 USDT is fine to BUY, but protecting 0.099 at a 43 stop is 4.26 < 5.
    v = _spot_venue(min_notional=5.0)
    assert v.plan_entry_quantity(0.1, entry_price=90.0, stop_price=43.0, side="long") == 0.0


def test_stop_price_rounding_never_increases_the_risk():
    v = _spot_venue(tick=0.5)
    assert v.round_stop(43.1, side="long") == pytest.approx(43.5)  # long stop rounds UP (closer to market)
    assert v.round_stop(43.1, side="short") == pytest.approx(43.0)  # short stop rounds DOWN


def test_stop_limit_price_sits_below_the_trigger_by_the_configured_buffer():
    import asyncio

    client = FakeBinance(market_type="spot")
    v = venue_mod.SpotVenue(client, "BTC/USDT", stop_limit_buffer_pct=1.0)
    asyncio.run(v.prepare())
    assert v.limit_price_for(100.0) == pytest.approx(99.0)


def test_order_normalization_reads_base_fees_from_fees_and_trades():
    raw = {"id": 7, "clientOrderId": "c", "status": "closed", "amount": 1, "filled": 1, "average": None, "cost": 100.0,
           "fees": [{"currency": "BTC", "cost": 0.001}, {"currency": "USDT", "cost": 0.1}]}
    info = venue_mod.order_from_ccxt(raw, base="BTC")
    assert info.fee_base == pytest.approx(0.001) and info.fee_known and info.average == pytest.approx(100.0)
    raw2 = {"id": 8, "status": "closed", "amount": 1, "filled": 1, "average": 10.0,
            "trades": [{"fee": {"currency": "BTC", "cost": 0.0005}}, {"fee": {"currency": "BTC", "cost": 0.0005}}]}
    assert venue_mod.order_from_ccxt(raw2, base="BTC").fee_base == pytest.approx(0.001)
    assert venue_mod.order_from_ccxt({"id": 9, "status": "closed", "filled": 1, "amount": 1}, base="BTC").fee_known is False


def test_spot_fee_in_base_means_stop_and_exit_quantities_never_exceed_what_is_held(db, monkeypatch):
    client, settings, account_id = open_protected(monkeypatch, market="spot", fee_rate=0.001)
    trade = the_trade(account_id)
    entry = client.orders_by_role("entry")[0]
    assert trade.entry_fee_base == pytest.approx(entry["filled"] * 0.001)
    assert trade.quantity <= entry["filled"] - trade.entry_fee_base + 1e-9
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "closed"  # a full-size sell would have raised InsufficientFunds


def test_spot_entry_without_fee_info_still_protects_a_safe_quantity(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client(fee_info=False)
    install(monkeypatch, client)
    account_id = new_account(settings)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.status == "open"
    assert trade.quantity <= client.orders_by_role("entry")[0]["filled"] * 0.999


# =========================== account-mode verification cache ===========================


def test_mode_verification_is_cached_only_for_managing_never_for_a_new_entry():
    import asyncio

    client = FakeBinance(market_type="futures")
    client.apiKey = "synthetic-key-for-cache-test"
    exchange._MODE_VERIFIED_AT.clear()
    asyncio.run(exchange.verify_futures_account_mode(client, client.symbol))  # entry-style: fresh read
    reads = sum(1 for c in client.calls if c[0] == "fetch_position_mode")
    assert reads == 1

    asyncio.run(exchange.verify_futures_account_mode(client, client.symbol, max_age_seconds=120))
    assert sum(1 for c in client.calls if c[0] == "fetch_position_mode") == 1, "served from the short cache"

    asyncio.run(exchange.verify_futures_account_mode(client, client.symbol))  # a new entry re-reads
    assert sum(1 for c in client.calls if c[0] == "fetch_position_mode") == 2


def test_a_failed_mode_verification_is_never_cached():
    import asyncio

    client = FakeBinance(market_type="futures", hedged=True)
    client.apiKey = "synthetic-key-for-cache-test-2"
    exchange._MODE_VERIFIED_AT.clear()
    with pytest.raises(exchange.UnsupportedAccountMode):
        asyncio.run(exchange.verify_futures_account_mode(client, client.symbol, max_age_seconds=120))
    client.hedged = False
    asyncio.run(exchange.verify_futures_account_mode(client, client.symbol, max_age_seconds=120))
    client.hedged = True
    with pytest.raises(exchange.UnsupportedAccountMode):
        asyncio.run(exchange.verify_futures_account_mode(client, client.symbol))  # fresh read sees the flip


def test_the_cache_key_is_a_digest_not_the_api_key():
    client = FakeBinance(market_type="futures")
    client.apiKey = "SECRET-API-KEY-VALUE"
    key = exchange._mode_cache_key(client)
    assert "SECRET" not in key and len(key) == 64


def test_close_position_rejection_falls_back_to_reduce_only_with_the_exchange_confirmed_size(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client(reject_close_position=True)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protective_kind == "stop_market_reduce"
    stop = client.orders_by_role("stop")[0]
    assert stop["reduce_only"] and not stop["close_position"]
    assert stop["amount"] == pytest.approx(abs(client.position)), "sized from the exchange position, not the bot's belief"
    assert client.violations == []


def test_stop_beyond_the_liquidation_price_is_not_a_protection(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client(liquidation_price=60.0)  # liquidated at 60, long stop at 43: the stop can never fire first
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "protection_failed_emergency_exit"
    assert client.orders_by_role("stop") == [] and client.position == 0.0


def test_realized_exit_prefers_the_newest_fills_so_a_previous_trade_cannot_pollute_pnl():
    import asyncio
    import time

    client = FakeBinance(market_type="futures")
    v = venue_mod.FuturesVenue(client, "BTC/USDT")
    now = int(time.time() * 1000)
    client.fills = [
        {"side": "sell", "amount": 0.2, "price": 10.0, "timestamp": now - 30_000},  # previous trade's exit
        {"side": "buy", "amount": 0.2, "price": 5.0, "timestamp": now - 20_000},  # an entry: wrong side, ignored
        {"side": "sell", "amount": 0.2, "price": 40.0, "timestamp": now},  # this trade's exit
    ]
    qty, vwap = asyncio.run(v.realized_exit(side="long", since_ms=now - 120_000, max_quantity=0.2))
    assert qty == pytest.approx(0.2) and vwap == pytest.approx(40.0)
    qty, vwap = asyncio.run(v.realized_exit(side="long", since_ms=now - 120_000, max_quantity=0.3))
    assert qty == pytest.approx(0.3) and vwap == pytest.approx((0.2 * 40 + 0.1 * 10) / 0.3)
    assert asyncio.run(v.realized_exit(side="short", since_ms=now - 120_000, max_quantity=0.2)) == (0.2, 5.0)
