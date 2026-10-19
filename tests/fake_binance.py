"""A stateful fake of the slice of ccxt (Binance Spot and USD-M Futures) that
mba_bot uses for live orders, with fault injection.

It models exactly the behaviours the protective-order design depends on, and
nothing it does is evidence about real Binance: every Binance semantic here
(duplicate client ids, reduceOnly rejection on a flat position, conditional
orders being invisible without `trigger=True`, a Spot base asset being
reserved by a resting stop, stop-limit gap-through, ...) is an ASSUMPTION
copied from the design and must be confirmed on Binance Testnet (see
ARCHITECTURE.md, "Testnet validation procedure"). What the fake does enforce
rigidly is *our* contract: a call shape that real ccxt 4.5.88 would not
accept (bare "BTC/USDT" on the Futures client, a conditional fetch/cancel
without trigger=True) fails here exactly as it would there.

Fault injection: `fake.fail("create_order", role="entry", when="after",
exc=ccxt.RequestTimeout("x"))` makes the next matching call raise. `when`:
  "before"  raise without any effect (the request never reached Binance)
  "after"   apply the effect, THEN raise (the response was lost)
`role` is derived from the deterministic client id the bot sends:
entry (...e), stop (...s<n>), exit (...x<n>).
"""
from __future__ import annotations

import itertools
import time

import ccxt.async_support as ccxt

FLAT_OHLCV = [[i, 100.0, 100.5, 99.5, 100.0, 1.0] for i in range(200)]


def _role(client_id: str | None) -> str | None:
    if not client_id or not client_id.startswith("mba") or len(client_id) < 28:
        return None
    return {"e": "entry", "s": "stop", "x": "exit"}.get(client_id[27])


class FakeBinance:
    _ids = itertools.count(1000)

    def __init__(
        self,
        *,
        market_type: str = "spot",
        pair: str = "BTC/USDT",
        price: float | None = None,
        ohlcv=None,
        ohlcv_by_pair=None,
        free_balance=None,
        leverage_by_symbol=None,
        leverage_raises: bool = False,
        hedged: bool = False,
        multi_assets: bool = False,
        portfolio_margin: bool = False,
        position_mode_raises: bool = False,
        order_types=None,
        step: float = 0.001,
        tick: float = 0.01,
        min_qty: float = 0.001,
        min_notional: float | None = 0.1,
        fee_rate: float = 0.001,
        fee_info: bool = True,
        liquidation_price: float | None = None,
        reject_close_position: bool = False,
        api_key: str | None = None,
        sandbox: bool = False,
    ):
        self.market_type = market_type
        self.pair = pair
        self.api_key = api_key
        self.sandbox = sandbox
        self.base, self.quote = pair.split("/")
        self.symbol = f"{pair}:{self.quote}" if market_type == "futures" else pair
        self._ohlcv = ohlcv
        self.ohlcv_by_pair = ohlcv_by_pair or {}
        self.price = price if price is not None else (ohlcv[-1][4] if ohlcv else 100.0)
        self._leverage_by_symbol = leverage_by_symbol or {}
        self._leverage_raises = leverage_raises
        self.hedged = hedged
        self.multi_assets = multi_assets
        self.portfolio_margin = portfolio_margin
        self.position_mode_raises = position_mode_raises
        self.step, self.tick, self.min_qty, self.min_notional = step, tick, min_qty, min_notional
        self.fee_rate, self.fee_info = fee_rate, fee_info
        self.liquidation_price = liquidation_price
        self.reject_close_position = reject_close_position
        if order_types is None:
            order_types = (
                ["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"]
                if market_type == "futures"
                else ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"]
            )
        self.order_types = list(order_types)

        free_balance = {"USDT": 50_000.0} if free_balance is None else dict(free_balance)
        self.free: dict[str, float] = dict(free_balance)
        self.used: dict[str, float] = {}
        self.position = 0.0  # futures, signed
        self.markets: dict | None = None
        self.options: dict = {}

        self.orders: dict[str, dict] = {}
        self.fills: list[dict] = []  # account trade history
        self.calls: list[tuple] = []
        self.violations: list[str] = []  # contract breaches the bot made
        self.faults: list[dict] = []
        self.on_cancel = None  # callable(fake, client_id) run right before a cancel is processed
        self.partial_entry_fill: float | None = None  # fraction of the NEXT market entry that fills
        self.closed = False
        self._last_ts = 0

        # The portfolio-margin probe only exists on the client when the key can call it.
        if not portfolio_margin:
            self.sapiGetPortfolioAccount = self._sapi_not_pm
        else:
            self.sapiGetPortfolioAccount = self._sapi_pm

    # ------------------------------------------------------------------ plumbing

    def fail(self, method: str, *, exc: Exception, role: str | None = None, when: str = "before", times: int = 1):
        self.faults.append({"method": method, "role": role, "exc": exc, "when": when, "times": times})

    def _fault(self, method: str, role: str | None, when: str):
        for rule in self.faults:
            if rule["method"] == method and rule["when"] == when and rule["times"] > 0 and rule["role"] in (None, role):
                rule["times"] -= 1
                raise rule["exc"]

    def _record(self, name: str, *args):
        self.calls.append((name, *args))

    async def close(self):
        self.closed = True

    async def load_markets(self):
        self._record("load_markets")
        filters = [
            {"filterType": "LOT_SIZE", "minQty": str(self.min_qty), "maxQty": "9000", "stepSize": str(self.step)},
            {"filterType": "MARKET_LOT_SIZE", "minQty": str(self.min_qty), "maxQty": "9000", "stepSize": str(self.step)},
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": str(self.tick)},
        ]
        if self.min_notional is not None:
            filters.append({"filterType": "NOTIONAL", "minNotional": str(self.min_notional)})
        market = {
            "id": self.base + self.quote,
            "symbol": self.symbol,
            "base": self.base,
            "quote": self.quote,
            "type": "swap" if self.market_type == "futures" else "spot",
            "spot": self.market_type == "spot",
            "swap": self.market_type == "futures",
            "future": False,
            "contract": self.market_type == "futures",
            "linear": True if self.market_type == "futures" else None,
            "active": True,
            "info": {"status": "TRADING", "orderTypes": self.order_types, "filters": filters},
        }
        self.markets = {self.symbol: market}
        return self.markets

    # ------------------------------------------------------------------ reads

    async def fetch_ohlcv(self, pair, timeframe=None, limit=None):
        self._record("fetch_ohlcv", pair)
        if self.market_type == "futures":
            assert pair.endswith(f":{self.quote}"), f"bare spot-style symbol {pair!r} sent to the Futures client"
        unified = pair.split(":")[0]
        if unified in self.ohlcv_by_pair:
            return self.ohlcv_by_pair[unified]
        return self._ohlcv if self._ohlcv is not None else FLAT_OHLCV

    async def fetch_ticker(self, symbol):
        return {"last": self.price}

    async def fetch_balance(self):
        self._record("fetch_balance")
        out: dict = {"free": dict(self.free), "info": {}}
        for asset in set(self.free) | set(self.used):
            out[asset] = {"free": self.free.get(asset, 0.0), "used": self.used.get(asset, 0.0)}
        return out

    async def fetch_leverage(self, symbol):
        self._record("fetch_leverage", symbol)
        if self._leverage_raises:
            raise RuntimeError("simulated Binance/ccxt leverage read failure")
        value = self._leverage_by_symbol.get(symbol)
        if value is None:
            value = self._leverage_by_symbol.get(symbol.split(":")[0])
        if value is None:
            return None
        return {"symbol": symbol, "longLeverage": value, "shortLeverage": value}

    async def fetch_position_mode(self, symbol=None):
        self._record("fetch_position_mode")
        if self.position_mode_raises:
            raise ccxt.ExchangeError("simulated position-mode read failure")
        return {"info": {}, "hedged": self.hedged}

    async def fapiPrivateGetMultiAssetsMargin(self):
        self._record("multi_assets")
        return {"multiAssetsMargin": "true" if self.multi_assets else "false"}

    async def _sapi_not_pm(self):
        raise ccxt.ExchangeError("not a portfolio margin account")

    async def _sapi_pm(self):
        return {"uniMMR": "5.0", "accountEquity": "1000"}

    async def fetch_positions(self, symbols=None):
        self._record("fetch_positions", tuple(symbols or ()))
        if self.market_type != "futures":
            raise ccxt.NotSupported("spot has no positions")
        return [
            {
                "symbol": self.symbol,
                "contracts": abs(self.position),
                "side": "long" if self.position > 0 else ("short" if self.position < 0 else None),
                "liquidationPrice": self.liquidation_price,
                "info": {"positionAmt": str(self.position), "positionSide": "BOTH"},
            }
        ]

    async def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        self._record("fetch_my_trades")
        return [dict(f) for f in self.fills if since is None or f["timestamp"] >= since]

    # ------------------------------------------------------------------ orders

    def _check_symbol(self, symbol: str):
        if symbol != self.symbol:
            raise ccxt.BadSymbol(f"fake does not have market symbol {symbol}")

    def _view(self, o: dict) -> dict:
        return {
            "id": o["id"],
            "clientOrderId": o["client_id"],
            "status": o["status"],
            "amount": o["amount"],
            "filled": o["filled"],
            "average": o["average"],
            "price": o.get("limit"),
            "stopPrice": o.get("trigger"),
            "fees": list(o.get("fees", [])),
            "symbol": self.symbol,
            "side": o["side"],
        }

    def _add_fill(self, side: str, amount: float, price: float):
        self._last_ts = max(int(time.time() * 1000), self._last_ts + 1)  # real, strictly increasing time
        self.fills.append({"side": side, "amount": amount, "price": price, "timestamp": self._last_ts, "symbol": self.symbol})

    def _lock_base(self, qty: float):
        if self.free.get(self.base, 0.0) + 1e-12 < qty:
            raise ccxt.InsufficientFunds("not enough base to reserve for the stop")
        self.free[self.base] -= qty
        self.used[self.base] = self.used.get(self.base, 0.0) + qty

    def _unlock_base(self, qty: float):
        self.used[self.base] = max(self.used.get(self.base, 0.0) - qty, 0.0)
        self.free[self.base] = self.free.get(self.base, 0.0) + qty

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = dict(params or {})
        client_id = params.get("clientOrderId")
        role = _role(client_id)
        self._record("create_order", symbol, type, side, amount, price, tuple(sorted(params)))
        self._check_symbol(symbol)
        self._fault("create_order", role, "before")

        conditional = any(k in params for k in ("stopLossPrice", "triggerPrice", "stopPrice"))
        if client_id in self.orders:
            raise ccxt.InvalidOrder("duplicate client order id")
        trigger = params.get("stopLossPrice") or params.get("triggerPrice") or params.get("stopPrice")

        if conditional:
            order = self._create_conditional(type, side, amount, price, params, client_id, trigger)
        else:
            order = self._create_market_or_limit(type, side, amount, params, client_id)
        self.orders[client_id] = order
        self._fault("create_order", role, "after")
        return self._view(order)

    def _new(self, **kw) -> dict:
        base = {
            "id": str(next(self._ids)), "filled": 0.0, "average": None, "status": "open",
            "fees": [], "limit": None, "trigger": None, "conditional": False, "reduce_only": False,
            "close_position": False, "kind": None,
        }
        base.update(kw)
        return base

    def _create_market_or_limit(self, type, side, amount, params, client_id):
        if type != "market":
            raise ccxt.InvalidOrder("fake only supports market orders here")
        reduce_only = bool(params.get("reduceOnly"))
        qty = float(amount)
        if self.market_type == "spot":
            return self._spot_market(side, qty, client_id)
        return self._futures_market(side, qty, reduce_only, client_id)

    def _spot_market(self, side, qty, client_id):
        price = self.price
        fraction = 1.0
        if side == "buy" and self.partial_entry_fill is not None:
            fraction, self.partial_entry_fill = self.partial_entry_fill, None
        filled = qty * fraction
        if side == "buy":
            cost = filled * price
            if self.free.get(self.quote, 0.0) + 1e-9 < cost:
                raise ccxt.InsufficientFunds("not enough quote")
            self.free[self.quote] -= cost
            fee = filled * self.fee_rate
            self.free[self.base] = self.free.get(self.base, 0.0) + filled - fee
            fees = [{"currency": self.base, "cost": fee}] if self.fee_info else []
        else:
            if self.free.get(self.base, 0.0) + 1e-9 < filled:
                raise ccxt.InsufficientFunds("not enough base to sell (reserved by a resting order?)")
            self.free[self.base] -= filled
            self.free[self.quote] = self.free.get(self.quote, 0.0) + filled * price * (1 - self.fee_rate)
            fees = [{"currency": self.quote, "cost": filled * price * self.fee_rate}] if self.fee_info else []
        self._add_fill(side, filled, price)
        status = "closed" if fraction >= 1.0 else "expired"
        return self._new(client_id=client_id, side=side, amount=qty, filled=filled, average=price, status=status, fees=fees)

    def _futures_market(self, side, qty, reduce_only, client_id):
        signed = qty if side == "buy" else -qty
        pos = self.position
        if reduce_only:
            if pos == 0 or (pos > 0) == (signed > 0):
                raise ccxt.InvalidOrder("ReduceOnly Order is rejected")
            qty = min(qty, abs(pos))
            signed = qty if side == "buy" else -qty
        fraction = 1.0
        if not reduce_only and self.partial_entry_fill is not None:
            fraction, self.partial_entry_fill = self.partial_entry_fill, None
        filled = qty * fraction
        signed = filled if side == "buy" else -filled
        new_pos = pos + signed
        if pos != 0 and new_pos != 0 and (pos > 0) != (new_pos > 0):
            self.violations.append(f"order {client_id} reversed the position {pos} -> {new_pos}")
        self.position = new_pos
        self._add_fill(side, filled, self.price)
        status = "closed" if fraction >= 1.0 else "expired"
        return self._new(
            client_id=client_id, side=side, amount=qty, filled=filled, average=self.price,
            status=status, reduce_only=reduce_only,
        )

    def _create_conditional(self, type, side, amount, price, params, client_id, trigger):
        trigger = float(trigger)
        wanted = "STOP_MARKET" if self.market_type == "futures" else ("STOP_LOSS" if type == "market" else "STOP_LOSS_LIMIT")
        if wanted not in self.order_types:
            raise ccxt.InvalidOrder(f"{wanted} is not a valid order type for this symbol")
        if side == "sell" and trigger >= self.price:
            raise ccxt.InvalidOrder("Order would immediately trigger")
        if side == "buy" and trigger <= self.price:
            raise ccxt.InvalidOrder("Order would immediately trigger")
        if self.market_type == "spot":
            qty = float(amount)
            self._lock_base(qty)
            return self._new(
                client_id=client_id, side=side, amount=qty, trigger=trigger, conditional=True,
                limit=float(price) if price is not None else None, kind=wanted,
            )
        close_position = bool(params.get("closePosition"))
        reduce_only = bool(params.get("reduceOnly"))
        if close_position and self.reject_close_position:
            raise ccxt.InvalidOrder("closePosition is not accepted here")
        if close_position and reduce_only:
            raise ccxt.InvalidOrder("closePosition and reduceOnly cannot both be set")
        if close_position and amount is not None:
            raise ccxt.InvalidOrder("quantity must be absent with closePosition")
        if not close_position and not reduce_only:
            # a bare conditional could OPEN a position: the contract forbids it
            self.violations.append(f"conditional {client_id} is neither closePosition nor reduceOnly")
        return self._new(
            client_id=client_id, side=side, amount=float(amount) if amount is not None else 0.0, trigger=trigger,
            conditional=True, close_position=close_position, reduce_only=reduce_only, kind=wanted,
        )

    def _lookup(self, client_id: str, trigger_flag: bool) -> dict:
        order = self.orders.get(client_id)
        if order is None:
            raise ccxt.OrderNotFound("no such order")
        if self.market_type == "futures" and order["conditional"] != bool(trigger_flag):
            # real ccxt routes to a different endpoint without trigger=True
            raise ccxt.OrderNotFound("not found on this endpoint")
        return order

    async def fetch_order(self, id, symbol=None, params=None):
        params = params or {}
        cid = params.get("clientOrderId") or params.get("clientAlgoId") or id
        self._record("fetch_order", cid, bool(params.get("trigger")))
        self._check_symbol(symbol)
        self._fault("fetch_order", _role(cid), "before")
        order = self._lookup(cid, params.get("trigger"))
        return self._view(order)

    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        params = params or {}
        self._record("fetch_open_orders", bool(params.get("trigger")))
        self._check_symbol(symbol)
        self._fault("fetch_open_orders", None, "before")
        want_conditional = bool(params.get("trigger"))
        return [
            self._view(o)
            for o in self.orders.values()
            if o["status"] == "open" and (o["conditional"] == want_conditional if self.market_type == "futures" else True)
        ]

    async def cancel_order(self, id, symbol=None, params=None):
        params = params or {}
        cid = params.get("clientOrderId") or params.get("clientAlgoId") or id
        self._record("cancel_order", cid, bool(params.get("trigger")))
        self._check_symbol(symbol)
        self._fault("cancel_order", _role(cid), "before")
        if self.on_cancel is not None:
            hook, self.on_cancel = self.on_cancel, None
            hook(self, cid)
        order = self._lookup(cid, params.get("trigger"))
        if order["status"] != "open":
            raise ccxt.OrderNotFound("order is not open")
        order["status"] = "canceled"
        if self.market_type == "spot" and order["conditional"]:
            self._unlock_base(order["amount"] - order["filled"])
        self._fault("cancel_order", _role(cid), "after")
        return self._view(order)

    # ------------------------------------------------------------------ market simulation

    def move_price(self, price: float):
        """Move the market; fires any resting conditional order whose trigger is crossed."""
        self.price = price
        for order in list(self.orders.values()):
            if order["status"] != "open" or not order["conditional"]:
                continue
            crossed = price <= order["trigger"] if order["side"] == "sell" else price >= order["trigger"]
            if not crossed:
                continue
            if self.market_type == "futures":
                self._fire_futures_stop(order)
            else:
                self._fire_spot_stop(order, price)

    def _fire_futures_stop(self, order):
        pos = self.position
        if pos == 0:
            order["status"] = "canceled"  # nothing to close
            return
        qty = abs(pos) if order["close_position"] else min(order["amount"], abs(pos))
        self.position = pos - (qty if pos > 0 else -qty)
        self._add_fill(order["side"], qty, self.price)
        order["status"] = "closed"  # ccxt: TRIGGERED/FINISHED -> "closed"

    def _fire_spot_stop(self, order, price):
        qty = order["amount"]
        if order["kind"] == "STOP_LOSS_LIMIT" and price < order["limit"]:
            order["triggered"] = True  # became a limit order the market has gapped through: never fills
            return
        fill_price = price
        self.used[self.base] = max(self.used.get(self.base, 0.0) - qty, 0.0)
        self.free[self.quote] = self.free.get(self.quote, 0.0) + qty * fill_price * (1 - self.fee_rate)
        self._add_fill("sell", qty, fill_price)
        order.update(status="closed", filled=qty, average=fill_price)

    def externally_close_position(self, price: float | None = None):
        """The customer (or Binance) flattens the position outside the bot."""
        if self.market_type == "futures":
            if self.position != 0:
                self._add_fill("sell" if self.position > 0 else "buy", abs(self.position), price or self.price)
            self.position = 0.0
            for o in self.orders.values():
                if o["conditional"] and o["status"] == "open":
                    pass  # Binance leaves closePosition stops resting; the bot must clean up
        else:
            self.free[self.base] = 0.0

    # ------------------------------------------------------------------ test conveniences

    def orders_by_role(self, role: str) -> list[dict]:
        return [o for o in self.orders.values() if _role(o["client_id"]) == role]

    @property
    def fetch_leverage_calls(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "fetch_leverage"]

    @property
    def fetched_pairs(self) -> list[str]:
        return [c[1].split(":")[0] for c in self.calls if c[0] == "fetch_ohlcv"]

    @property
    def market_orders(self) -> list[tuple[str, str, float]]:
        """Every non-conditional order the bot sent: (symbol, side, qty)."""
        return [(self.symbol, o["side"], o["amount"]) for o in self.orders.values() if not o["conditional"]]
