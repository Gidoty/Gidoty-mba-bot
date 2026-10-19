"""Pins what the bot sends through the REAL ccxt 4.5.88 for protective
stops, exits and lookups.

These tests run the real `ccxt.async_support.binance`/`binanceusdm` classes
with synthetic markets and replace only the network-level endpoint methods,
so they check that venue.py's calls are turned into the intended Binance
requests (STOP_MARKET via the Algo Order endpoint, reduceOnly/closePosition
flags, client ids, trigger=True routing, tick/step rounding). They say
nothing about whether Binance ACCEPTS those requests: that is what the
Testnet validation procedure in ARCHITECTURE.md is for."""
from __future__ import annotations

import asyncio
import pathlib

import ccxt
import ccxt.async_support as cx
import pytest

from mba_bot import venue as venue_mod


def _market(spot: bool, order_types):
    filters = [
        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "500", "stepSize": "0.001"},
        {"filterType": "PRICE_FILTER", "minPrice": "0.1", "maxPrice": "1000000", "tickSize": "0.1"},
        {"filterType": "NOTIONAL", "minNotional": "5"},
    ]
    common = {
        "id": "BTCUSDT", "base": "BTC", "quote": "USDT", "baseId": "BTC", "quoteId": "USDT", "active": True,
        "precision": {"amount": 0.001, "price": 0.1}, "limits": {"amount": {}, "price": {}, "cost": {}, "leverage": {}},
        "margin": False, "future": False, "option": False,
        "info": {"status": "TRADING", "orderTypes": order_types, "filters": filters},
    }
    if spot:
        return {**common, "symbol": "BTC/USDT", "type": "spot", "spot": True, "swap": False, "contract": False,
                "linear": None, "inverse": None, "settle": None}
    return {**common, "symbol": "BTC/USDT:USDT", "type": "swap", "spot": False, "swap": True, "contract": True,
            "linear": True, "inverse": False, "settle": "USDT", "settleId": "USDT"}


FUT_TYPES = ["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"]
SPOT_TYPES = ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"]


class Recorder:
    def __init__(self, client):
        self.calls: list[tuple[str, dict]] = []
        self.client = client

    def stub(self, name, response):
        async def _fn(request=None, *a, **k):
            self.calls.append((name, dict(request or {})))
            return response() if callable(response) else response

        setattr(self.client, name, _fn)

    def last(self, name):
        return [req for n, req in self.calls if n == name][-1]


def _futures():
    client = cx.binanceusdm({"apiKey": "k", "secret": "s"})
    client.set_markets([_market(False, FUT_TYPES)])
    rec = Recorder(client)
    rec.stub("fapiPrivateGetPositionSideDual", {"dualSidePosition": False})
    rec.stub("fapiPrivateGetMultiAssetsMargin", {"multiAssetsMargin": False})
    algo = {"algoId": 3358, "clientAlgoId": "mbaX", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET", "symbol": "BTCUSDT",
            "side": "SELL", "algoStatus": "NEW", "triggerPrice": "43.0", "closePosition": True, "reduceOnly": False}
    rec.stub("fapiPrivatePostAlgoOrder", algo)
    rec.stub("fapiPrivateGetAlgoOrder", algo)
    rec.stub("fapiPrivateDeleteAlgoOrder", {"algoId": 3358, "clientAlgoId": "mbaX", "code": "200", "msg": "success"})
    rec.stub("fapiPrivateGetOpenAlgoOrders", [algo])
    rec.stub("fapiPrivatePostOrder", {"orderId": 5, "clientOrderId": "mbaY", "status": "FILLED", "symbol": "BTCUSDT",
                                      "executedQty": "0.2", "origQty": "0.2", "avgPrice": "84", "side": "SELL", "type": "MARKET"})
    return client, rec


def _run(client, venue_cls, body, **venue_kw):
    """Run `body(venue)` (an async function) in ONE event loop after
    `prepare()`: ccxt's throttler binds to the loop it first runs in, so
    separate asyncio.run calls break it."""

    async def go():
        v = venue_cls(client, "BTC/USDT", **venue_kw)
        await v.prepare()
        try:
            return await body(v)
        finally:
            await client.close()

    return asyncio.run(go())


def test_ccxt_is_pinned_to_the_validated_version():
    assert ccxt.__version__ == "4.5.88"
    reqs = (pathlib.Path(__file__).parent.parent / "requirements.txt").read_text()
    assert "ccxt==4.5.88" in reqs and "ccxt>=" not in reqs


def test_futures_protective_stop_is_a_close_position_stop_market_via_the_algo_endpoint():
    client, rec = _futures()

    async def body(v):
        return await v.place_protection(side="long", quantity=0.2, stop_price=43.04, client_id="mbaX")

    prot = _run(client, venue_mod.FuturesVenue, body)
    req = rec.last("fapiPrivatePostAlgoOrder")
    assert req["type"] == "STOP_MARKET" and req["algoType"] == "CONDITIONAL"
    assert req["side"] == "SELL" and req["symbol"] == "BTCUSDT"
    assert req["closePosition"] is True and "quantity" not in req and "reduceOnly" not in req
    assert req["triggerPrice"] == "43.1", "long stop is tick-aligned UP, never to a lower (riskier) trigger"
    assert req["workingType"] == "MARK_PRICE" and req["clientAlgoId"] == "mbaX"
    assert prot.kind == "stop_market_close" and prot.state == "open"
    assert "fapiPrivatePostOrder" not in [n for n, _ in rec.calls], "conditional orders must not use the regular order endpoint"


def test_futures_short_protection_buys_and_rounds_the_trigger_down():
    client, rec = _futures()

    async def body(v):
        await v.place_protection(side="short", quantity=0.2, stop_price=143.06, client_id="mbaX")

    _run(client, venue_mod.FuturesVenue, body)
    req = rec.last("fapiPrivatePostAlgoOrder")
    assert req["side"] == "BUY" and float(req["triggerPrice"]) == 143.0


def test_futures_exit_is_a_reduce_only_market_order_with_a_floored_quantity():
    client, rec = _futures()

    async def body(v):
        return await v.place_exit("long", 0.2129, "mbaY")

    order = _run(client, venue_mod.FuturesVenue, body)
    req = rec.last("fapiPrivatePostOrder")
    assert req["type"] == "MARKET" and req["side"] == "SELL" and req["reduceOnly"] is True
    assert req["quantity"] == "0.212" and req["newClientOrderId"] == "mbaY"
    assert client.urlencode({"reduceOnly": req["reduceOnly"]}) == "reduceOnly=true"
    assert order.filled == pytest.approx(0.2) and order.average == pytest.approx(84.0)


def test_futures_entry_is_a_plain_market_order_with_the_deterministic_client_id():
    client, rec = _futures()

    async def body(v):
        await v.place_entry("long", 0.212, "mbaZ")

    _run(client, venue_mod.FuturesVenue, body, entries_allowed=True)  # acting as the entry path
    req = rec.last("fapiPrivatePostOrder")
    assert req["type"] == "MARKET" and req["side"] == "BUY" and req["newClientOrderId"] == "mbaZ"
    assert "reduceOnly" not in req and "stopPrice" not in req


def test_futures_conditional_lookup_cancel_and_listing_use_the_algo_endpoints_with_trigger_true():
    client, rec = _futures()
    rec.stub("fapiPrivateGetOpenOrders", [])

    async def body(v):
        got = await v.get_protection(kind=None, client_id="mbaX")
        await v.cancel_by_client_id("mbaX", conditional=True)
        listed = await v.list_open_bot_orders()
        return got, listed

    got, listed = _run(client, venue_mod.FuturesVenue, body)
    assert rec.last("fapiPrivateGetAlgoOrder")["clientAlgoId"] == "mbaX" and got.state == "open"
    assert rec.last("fapiPrivateDeleteAlgoOrder")["clientAlgoId"] == "mbaX"
    assert ("mbaX", True) in listed
    assert "fapiPrivateGetOpenAlgoOrders" in [n for n, _ in rec.calls]


@pytest.mark.parametrize("algo_status, expected", [("NEW", "open"), ("TRIGGERED", "triggered"), ("FINISHED", "triggered"),
                                                    ("CANCELED", "canceled"), ("EXPIRED", "expired"), ("REJECTED", "rejected")])
def test_algo_order_statuses_map_to_the_protection_states_the_lifecycle_uses(algo_status, expected):
    client, rec = _futures()
    algo = {"algoId": 1, "clientAlgoId": "mbaX", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET", "symbol": "BTCUSDT",
            "side": "SELL", "algoStatus": algo_status, "triggerPrice": "43.0"}
    rec.stub("fapiPrivateGetAlgoOrder", algo)

    async def body(v):
        return await v.get_protection(kind=None, client_id="mbaX")

    assert _run(client, venue_mod.FuturesVenue, body).state == expected


def _spot(types):
    client = cx.binance({"apiKey": "k", "secret": "s"})
    client.set_markets([_market(True, types)])
    rec = Recorder(client)
    rec.stub("privatePostOrder", {"orderId": 9, "clientOrderId": "mbaS", "status": "NEW", "symbol": "BTCUSDT",
                                  "type": "STOP_LOSS", "side": "SELL", "origQty": "0.211", "executedQty": "0"})
    return client, rec


def test_spot_market_stop_request_shape():
    client, rec = _spot(SPOT_TYPES)

    async def body(v):
        assert v.kind == "stop_loss"
        await v.place_protection(side="long", quantity=0.2119, stop_price=43.04, client_id="mbaS")

    _run(client, venue_mod.SpotVenue, body)
    req = rec.last("privatePostOrder")
    assert req["type"] == "STOP_LOSS" and req["side"] == "SELL" and req["quantity"] == "0.211"
    assert req["stopPrice"] == "43.1" and req["newClientOrderId"] == "mbaS" and "price" not in req


def test_spot_stop_limit_request_shape_when_only_stop_limit_is_listed():
    client, rec = _spot(["LIMIT", "MARKET", "STOP_LOSS_LIMIT"])

    async def body(v):
        assert v.kind == "stop_loss_limit"
        await v.place_protection(side="long", quantity=0.2, stop_price=100.0, client_id="mbaS")

    _run(client, venue_mod.SpotVenue, body, stop_limit_buffer_pct=0.5)
    req = rec.last("privatePostOrder")
    assert req["type"] == "STOP_LOSS_LIMIT" and float(req["stopPrice"]) == 100.0 and float(req["price"]) == 99.5
    assert req["timeInForce"] == "GTC" and req["quantity"] == "0.2"


def test_spot_entry_and_exit_are_plain_market_orders_by_base_quantity():
    client, rec = _spot(SPOT_TYPES)
    rec.stub("privatePostOrder", {"orderId": 1, "clientOrderId": "mbaE", "status": "FILLED", "symbol": "BTCUSDT", "side": "BUY",
                                  "type": "MARKET", "origQty": "0.212", "executedQty": "0.212", "cummulativeQuoteQty": "19.08",
                                  "fills": [{"price": "90", "qty": "0.212", "commission": "0.000212", "commissionAsset": "BTC"}]})

    async def body(v):
        order = await v.place_entry("long", 0.212, "mbaE")
        entry_req = rec.last("privatePostOrder")
        await v.place_exit("long", 0.2119, "mbaX")
        return order, entry_req, v.net_from_fee(order.filled, order.fee_base)

    order, entry_req, net = _run(client, venue_mod.SpotVenue, body, entries_allowed=True)  # acting as the entry path
    assert entry_req["type"] == "MARKET" and entry_req["quantity"] == "0.212" and "quoteOrderQty" not in entry_req
    assert order.fee_base == pytest.approx(0.000212) and order.filled == pytest.approx(0.212)
    assert net == pytest.approx(0.211)
    assert rec.last("privatePostOrder")["quantity"] == "0.211"


def test_ccxt_itself_rejects_a_stop_type_the_symbol_does_not_list():
    """The capability gate is double-checked by ccxt: STOP_LOSS on a symbol
    whose orderTypes lack it raises InvalidOrder before any request."""
    client, rec = _spot(["LIMIT", "MARKET", "STOP_LOSS_LIMIT"])

    async def go():
        try:
            await client.create_order("BTC/USDT", "market", "sell", 0.2, None, {"stopLossPrice": 43.0})
        finally:
            await client.close()

    with pytest.raises(cx.InvalidOrder):
        asyncio.run(go())
    assert rec.calls == []
