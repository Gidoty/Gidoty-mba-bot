"""Exchange-order layer: entry/exit orders, exchange-side protective stops,
and reads of exchange truth, for Binance Spot and Binance USD-M Futures.

Everything that talks to ccxt about *orders* lives here, behind one small
interface (`Venue`), so that `lifecycle.py` (the state machine) never
touches ccxt directly and tests can swap in a fake exchange. This module
holds no database state and makes no decisions about what a trade should do
next; it places, finds, cancels and reads orders, and normalizes what comes
back.

Design rules (see ARCHITECTURE.md, "Exchange-side protective stops"):

* Nothing is assumed about Binance. Order types and filters are read from
  the loaded market of the actual symbol/environment (`prepare`); a missing
  capability raises `ProtectionUnavailable` and the caller refuses to enter.
* Every order carries a deterministic client order id supplied by the
  caller, so a retry after a lost response finds/duplicates nothing.
* Futures orders can never increase or reverse a position: exits are
  `reduceOnly`, protective stops are `closePosition` (or `reduceOnly` with
  an exchange-confirmed quantity).
* Futures conditional (STOP_MARKET) orders go through ccxt 4.5.88's Binance
  Algo Order path, which requires `trigger=True` to fetch/cancel/list them.
  Tests/test_ccxt_contract.py pins that request shape.
* Error text from ccxt/Binance is never logged or returned to customers
  from here; callers log exception *types* only.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import ccxt.async_support as ccxt_async

from . import exchange

logger = logging.getLogger(__name__)

CLIENT_ID_PREFIX = "mba"

# How long a successful Futures account-mode verification may be reused while
# MANAGING an open trade (never for a new entry).
MODE_CACHE_SECONDS = 120

# Protective mechanisms recorded on Trade.protective_kind.
KIND_FUTURES_CLOSE = "stop_market_close"
KIND_FUTURES_REDUCE = "stop_market_reduce"
KIND_SPOT_MARKET = "stop_loss"
KIND_SPOT_LIMIT = "stop_loss_limit"
CONDITIONAL_KINDS = (KIND_FUTURES_CLOSE, KIND_FUTURES_REDUCE)

# PRE-TRADE FILTERING ONLY. Headroom applied when deciding whether a planned
# Spot entry is big enough that what is left after commission could still be
# protected (minimum quantity / notional). It is NOT a statement about
# Binance fees and is never used to determine a quantity that is protected
# or sold: that comes only from exchange-confirmed commission data
# (SpotVenue.resolve_entry_fee_base) bounded by the available balance.
SPOT_ENTRY_HEADROOM = 0.002

# Used for bounded polling; patched out in tests.
_sleep = asyncio.sleep


class ProtectionUnavailable(RuntimeError):
    """The exchange-side protective stop this venue needs is not available
    (order type not listed for the symbol, missing/ambiguous filters, the
    quantity or notional cannot satisfy them). No entry is placed."""


class EntryForbidden(RuntimeError):
    """An entry was attempted on a Venue that is not entry-capable. Venues
    are built risk-reducing-only by default; only the new-entry path, after
    its fresh credential validation (exchange.assert_trade_only), builds one
    with entries_allowed=True."""


class QuantityUnresolvable(RuntimeError):
    """The base quantity the bot actually holds from a Spot fill could not be
    established from exchange-confirmed commission data. The caller must not
    guess: it takes the emergency safety path instead."""


class ProtectionStillOpen(RuntimeError):
    """A cancel was requested but the protective order could not be
    confirmed terminal. The caller must not assume exposure is unprotected
    nor that it is free to exit on the Spot side."""


# --- normalized shapes ------------------------------------------------------


@dataclass(frozen=True)
class OrderInfo:
    """A regular (non-conditional) order, normalized from a ccxt order."""

    order_id: str | None
    client_id: str | None
    status: str  # ccxt: open | closed | canceled | expired | rejected | ...
    amount: float
    filled: float
    average: float | None
    fee_base: float  # commission taken in the BASE asset (Spot buys)
    fee_known: bool  # False if the response carried no fee information at all

    @property
    def is_terminal(self) -> bool:
        return self.status in ("closed", "canceled", "expired", "rejected")


@dataclass(frozen=True)
class ProtectionOrder:
    """A resting protective stop, normalized.

    `state`:
      open       resting at Binance, nothing executed
      partial    Spot only: a triggered stop-limit has partly filled
      filled     Spot only: fully executed
      triggered  Futures only: the conditional was triggered (ccxt maps
                 Binance TRIGGERED/FINISHED to "closed"); fill details are
                 NOT carried by the conditional and come from exposure and
                 trade history
      canceled / expired / rejected   terminal, nothing (more) will execute
    """

    client_id: str
    order_id: str | None
    kind: str
    state: str
    filled: float
    average: float | None
    stop_price: float | None
    limit_price: float | None

    @property
    def is_resting(self) -> bool:
        return self.state in ("open", "partial")


@dataclass(frozen=True)
class MarketRules:
    step: float
    min_qty: float
    max_qty: float | None
    market_step: float
    market_min_qty: float
    market_max_qty: float | None
    tick: float
    min_notional: float | None
    order_types: frozenset


def _d(value: float | str) -> Decimal:
    return Decimal(str(value))


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return float((_d(value) / _d(step)).to_integral_value(rounding=ROUND_FLOOR) * _d(step))


def ceil_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return float((_d(value) / _d(step)).to_integral_value(rounding=ROUND_CEILING) * _d(step))


def _f(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _filter(filters: list, name: str) -> dict | None:
    for entry in filters or []:
        if isinstance(entry, dict) and entry.get("filterType") == name:
            return entry
    return None


def rules_from_market(market: dict) -> MarketRules:
    """Reads LOT_SIZE / MARKET_LOT_SIZE / PRICE_FILTER / (MIN_)NOTIONAL and
    the allowed order types from the loaded market's exchangeInfo entry.
    Anything required but missing raises ProtectionUnavailable: we never
    substitute a guessed precision for a filter Binance did not give us."""
    info = market.get("info") if isinstance(market.get("info"), dict) else {}
    filters = info.get("filters") or []
    lot = _filter(filters, "LOT_SIZE")
    price = _filter(filters, "PRICE_FILTER")
    if not lot or not price:
        raise ProtectionUnavailable("symbol filters (LOT_SIZE/PRICE_FILTER) are not available")
    step = _f(lot.get("stepSize"))
    min_qty = _f(lot.get("minQty"))
    tick = _f(price.get("tickSize"))
    if not step or step <= 0 or min_qty is None or not tick or tick <= 0:
        raise ProtectionUnavailable("symbol filters are unreadable")
    max_qty = _f(lot.get("maxQty"))
    mlot = _filter(filters, "MARKET_LOT_SIZE") or {}
    m_step = _f(mlot.get("stepSize")) or step
    if m_step <= 0:
        m_step = step
    m_min = _f(mlot.get("minQty"))
    m_max = _f(mlot.get("maxQty"))
    if m_max is not None and m_max <= 0:
        m_max = None  # Binance uses 0 for "no limit"
    if max_qty is not None and max_qty <= 0:
        max_qty = None

    notional_filter = _filter(filters, "NOTIONAL") or _filter(filters, "MIN_NOTIONAL")
    min_notional = None
    if notional_filter:
        min_notional = _f(notional_filter.get("minNotional")) or _f(notional_filter.get("notional"))

    order_types = frozenset(str(t).upper() for t in (info.get("orderTypes") or []))
    return MarketRules(
        step=step,
        min_qty=min_qty,
        max_qty=max_qty,
        market_step=m_step,
        market_min_qty=m_min if m_min is not None else min_qty,
        market_max_qty=m_max,
        tick=tick,
        min_notional=min_notional,
        order_types=order_types,
    )


def order_from_ccxt(order: Any, *, base: str | None = None) -> OrderInfo:
    if not isinstance(order, dict):
        raise ValueError("unexpected order shape")
    filled = _f(order.get("filled")) or 0.0
    amount = _f(order.get("amount")) or 0.0
    average = _f(order.get("average"))
    if average is None and filled > 0:
        cost = _f(order.get("cost"))
        if cost is not None:
            average = cost / filled

    fee_entries: list[dict] = []
    fees = order.get("fees")
    if isinstance(fees, list):
        fee_entries.extend(f for f in fees if isinstance(f, dict))
    fee = order.get("fee")
    if isinstance(fee, dict):
        fee_entries.append(fee)
    for trade in order.get("trades") or []:
        if isinstance(trade, dict):
            tf = trade.get("fee")
            if isinstance(tf, dict):
                fee_entries.append(tf)
            for f in trade.get("fees") or []:
                if isinstance(f, dict):
                    fee_entries.append(f)
    # ccxt may report the same fee in both `fee`/`fees` and per-trade; take
    # the per-order aggregate when present, else the per-trade sum.
    aggregate = [f for f in (fees if isinstance(fees, list) else []) if isinstance(f, dict)] or (
        [fee] if isinstance(fee, dict) else []
    )
    chosen = aggregate or fee_entries
    fee_base = 0.0
    for entry in chosen:
        if base is not None and entry.get("currency") == base:
            fee_base += _f(entry.get("cost")) or 0.0
    fee_known = bool(chosen)

    oid = order.get("id")
    cid = order.get("clientOrderId")
    return OrderInfo(
        order_id=str(oid) if oid is not None else None,
        client_id=str(cid) if cid is not None else None,
        status=str(order.get("status")),
        amount=amount,
        filled=filled,
        average=average,
        fee_base=fee_base,
        fee_known=fee_known,
    )


def _bot_owned(client_id: str | None) -> bool:
    return bool(client_id) and str(client_id).startswith(CLIENT_ID_PREFIX)


# --- venue ------------------------------------------------------------------


class Venue:
    """Common behaviour; Spot/Futures subclasses implement the rest."""

    market_type: str = ""

    def __init__(self, client: ccxt_async.Exchange, pair: str, *, entries_allowed: bool = False):
        self.client = client
        self.pair = pair
        # False = risk-reducing only (everything except place_entry works).
        self.entries_allowed = entries_allowed
        self.symbol = exchange.exchange_symbol(pair, self.market_type)
        self.base = pair.split("/", 1)[0]
        self.market: dict | None = None
        self.rules: MarketRules | None = None

    # -- setup / capability -------------------------------------------------

    async def prepare(self, *, managing: bool = False) -> None:
        """Load markets and verify everything a live entry on this symbol
        depends on. Raises UnsupportedSymbol / UnsupportedAccountMode /
        ProtectionUnavailable (all mean: place no order). `managing=True`
        (an already-open trade) lets the slow account-mode reads be served
        from a short cache; a NEW entry always re-reads them."""
        await self.client.load_markets()
        self.market = exchange.verify_market(self.client, self.symbol, market_type=self.market_type)
        self.rules = rules_from_market(self.market)
        if "MARKET" not in self.rules.order_types:
            raise ProtectionUnavailable("MARKET orders are not listed for this symbol")
        await self._verify_capabilities(managing=managing)

    async def _verify_capabilities(self, *, managing: bool = False) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def default_kind(self) -> str:
        raise NotImplementedError

    def _require_ready(self) -> MarketRules:
        if self.rules is None:
            raise RuntimeError("Venue.prepare() has not run")
        return self.rules

    # -- sizing ---------------------------------------------------------------

    def round_stop(self, stop_price: float, *, side: str) -> float:
        """Tick-align the trigger so the realized risk is never LARGER than
        planned: a long's (lower) stop rounds up, a short's rounds down."""
        rules = self._require_ready()
        if side == "long":
            return ceil_to_step(stop_price, rules.tick)
        return floor_to_step(stop_price, rules.tick)

    def plan_entry_quantity(self, quantity: float, *, entry_price: float, stop_price: float, side: str) -> float:
        """Entry quantity floored to the market step and capped at the
        market max; 0.0 if it cannot satisfy minimum quantity/notional
        (and, for Spot, if the quantity left AFTER the base commission could
        not be protected). 0.0 means: place no order."""
        raise NotImplementedError

    # -- orders ---------------------------------------------------------------

    async def place_entry(self, side: str, quantity: float, client_id: str) -> OrderInfo:
        if not self.entries_allowed:
            raise EntryForbidden("this venue is risk-reducing only")
        order_side = "buy" if side == "long" else "sell"
        raw = await self.client.create_order(
            self.symbol, "market", order_side, quantity, None, {"clientOrderId": client_id}
        )
        return order_from_ccxt(raw, base=self.base)

    async def find_order(self, client_id: str) -> OrderInfo | None:
        """The regular order with this client id, or None if Binance has no
        such order (ccxt OrderNotFound). Any other error propagates: an
        unreadable answer is NOT "no such order"."""
        try:
            raw = await self.client.fetch_order(client_id, self.symbol, {"clientOrderId": client_id})
        except ccxt_async.OrderNotFound:
            return None
        return order_from_ccxt(raw, base=self.base)

    async def await_final(self, client_id: str, *, attempts: int, delay: float) -> OrderInfo | None:
        """Poll a regular order until it is terminal or attempts run out;
        returns the last state seen (None if never found)."""
        last: OrderInfo | None = None
        for i in range(max(1, attempts)):
            last = await self.find_order(client_id)
            if last is not None and last.is_terminal:
                return last
            if i + 1 < attempts:
                await _sleep(delay)
        return last

    async def list_open_bot_orders(self) -> list[tuple[str, bool]]:
        """(client_id, is_conditional) for every open order whose client id
        carries the bot prefix. Orders the customer placed themselves are
        never touched."""
        raise NotImplementedError

    async def cancel_by_client_id(self, client_id: str, *, conditional: bool) -> None:
        params = {"clientOrderId": client_id}
        if conditional:
            params["trigger"] = True
        try:
            await self.client.cancel_order(client_id, self.symbol, params)
        except ccxt_async.OrderNotFound:
            return

    # -- protective stop ------------------------------------------------------

    async def protection_precheck(self, *, side: str, stop_price: float) -> None:
        """Last read-only sanity check before a stop is placed. Raises
        ProtectionUnavailable if the stop could not protect (default: ok)."""
        return None

    async def place_protection(
        self, *, side: str, quantity: float, stop_price: float, client_id: str, entry_price: float | None = None
    ) -> ProtectionOrder:
        raise NotImplementedError

    async def get_protection(self, *, kind: str, client_id: str) -> ProtectionOrder | None:
        raise NotImplementedError

    async def cancel_protection(self, *, kind: str, client_id: str) -> ProtectionOrder | None:
        """Cancel, then read the final state. Returns that state (which may
        be "filled"/"triggered" if the stop won the race), or None if the
        order is not retrievable. Raises ProtectionStillOpen if, after the
        cancel, it still reads as resting."""
        conditional = kind in CONDITIONAL_KINDS
        try:
            await self.cancel_by_client_id(client_id, conditional=conditional)
        except (ccxt_async.NetworkError, ccxt_async.ExchangeError):
            # A cancel that errored (including "unknown order" semantics
            # beyond OrderNotFound) is not proof of anything: read the state.
            pass
        final = await self.get_protection(kind=kind, client_id=client_id)
        if final is not None and final.is_resting:
            raise ProtectionStillOpen("protective order still resting after cancel")
        return final

    # -- exits / exchange truth ------------------------------------------------

    async def place_exit(self, side: str, quantity: float, client_id: str) -> OrderInfo:
        raise NotImplementedError

    async def protectable_quantity(self, quantity: float) -> float:
        return quantity  # Futures: closePosition / exchange-read size; nothing to clamp

    async def exposure(self) -> float:
        """Exchange-confirmed quantity currently held in the trade's
        direction, >= 0. Futures: |position| if its sign matches `side` of
        the caller (see FuturesVenue.signed_position); Spot: free base."""
        raise NotImplementedError


# --- Spot ---------------------------------------------------------------------


class SpotVenue(Venue):
    market_type = "spot"

    def __init__(self, client, pair, *, stop_limit_buffer_pct: float = 0.5, entries_allowed: bool = False):
        super().__init__(client, pair, entries_allowed=entries_allowed)
        self.stop_limit_buffer_pct = stop_limit_buffer_pct
        self.kind: str | None = None

    async def _verify_capabilities(self, *, managing: bool = False) -> None:
        rules = self._require_ready()
        info = self.market.get("info") if isinstance(self.market.get("info"), dict) else {}
        status = info.get("status")
        if status is not None and str(status).upper() != "TRADING":
            raise ProtectionUnavailable("symbol is not in TRADING status")
        if info.get("isSpotTradingAllowed") is False:
            raise ProtectionUnavailable("spot trading is not allowed for this symbol")
        # Prefer the market-triggered stop (guaranteed exit once triggered,
        # slippage accepted) over the stop-LIMIT (price-capped, but may not
        # fill in a gap-through). Never assume either is listed.
        if "STOP_LOSS" in rules.order_types:
            self.kind = KIND_SPOT_MARKET
        elif "STOP_LOSS_LIMIT" in rules.order_types:
            self.kind = KIND_SPOT_LIMIT
        else:
            raise ProtectionUnavailable("no supported protective stop order type is listed for this symbol")

    @property
    def default_kind(self) -> str:
        return self.kind or KIND_SPOT_MARKET

    def limit_price_for(self, stop_price: float) -> float:
        rules = self._require_ready()
        raw = stop_price * (1.0 - self.stop_limit_buffer_pct / 100.0)
        return floor_to_step(raw, rules.tick)

    def net_from_fee(self, filled: float, fee_base: float) -> float:
        """Base quantity the bot holds from a fill: filled minus the
        base-asset commission, floored to the lot step. `fee_base` must be
        an exchange-confirmed figure (0.0 when the commission was charged in
        another asset); this method never invents one."""
        rules = self._require_ready()
        return floor_to_step(max(filled - fee_base, 0.0), rules.step)

    async def resolve_entry_fee_base(self, order: OrderInfo) -> float:
        """Base-asset commission of the entry fill, from exchange-confirmed
        data only, in this order:

        1. the order response itself (`fees` / per-fill `fee`) - a response
           that lists fees in another asset (quote, BNB) confirms a base
           commission of 0.0;
        2. otherwise the account's own trade records for that order id
           (`fetch_my_trades(orderId=...)`), accepted only if they carry a
           fee for every fill AND account for the whole filled quantity.

        Anything else raises QuantityUnresolvable. A network error while
        reading the trade records also raises it: after bounded retries by
        the caller, not knowing the commission is treated as not being able
        to establish a protectable quantity."""
        if order.fee_known:
            return order.fee_base
        if not order.order_id:
            raise QuantityUnresolvable("no order id to look up the fills of")
        try:
            trades = await self.client.fetch_my_trades(self.symbol, None, None, {"orderId": order.order_id})
        except Exception as exc:
            raise QuantityUnresolvable(f"could not read the fills ({type(exc).__name__})") from exc
        mine = [t for t in trades or [] if isinstance(t, dict) and str(t.get("order")) == str(order.order_id)]
        if not mine:
            raise QuantityUnresolvable("no fills found for the entry order")
        covered = 0.0
        fee_base = 0.0
        for t in mine:
            fee = t.get("fee")
            if not isinstance(fee, dict) or fee.get("cost") is None or not fee.get("currency"):
                raise QuantityUnresolvable("a fill carries no commission data")
            covered += _f(t.get("amount")) or 0.0
            if fee.get("currency") == self.base:
                fee_base += _f(fee.get("cost")) or 0.0
        if covered + self._require_ready().step < order.filled:
            raise QuantityUnresolvable("fills do not account for the whole filled quantity")
        return fee_base

    async def protectable_quantity(self, quantity: float) -> float:
        """The most of `quantity` (the bot-attributable base quantity) that
        can be protected right now: never more than the base actually
        FREE on the exchange. The wallet may hold the customer's own units
        of the same asset, so the free balance is only ever an upper bound,
        never the amount to protect."""
        rules = self._require_ready()
        free = await self.exposure()
        return floor_to_step(min(quantity, free), rules.step)

    def can_protect(self, net_quantity: float, stop_price: float) -> bool:
        rules = self._require_ready()
        if net_quantity < rules.min_qty or net_quantity <= 0:
            return False
        reference = self.limit_price_for(stop_price) if self.kind == KIND_SPOT_LIMIT else stop_price
        if rules.min_notional is not None and net_quantity * reference < rules.min_notional:
            return False
        return True

    def can_sell(self, quantity: float, price: float) -> bool:
        """Could a MARKET sell of `quantity` be accepted at all (minimum
        quantity and, where Binance applies it to market orders, minimum
        notional)? False means the holding is dust the exchange won't trade."""
        rules = self._require_ready()
        if quantity < max(rules.market_min_qty, rules.min_qty) or quantity <= 0:
            return False
        if rules.min_notional is not None and quantity * price < rules.min_notional:
            return False
        return True

    def plan_entry_quantity(self, quantity, *, entry_price, stop_price, side):
        rules = self._require_ready()
        if side != "long":
            return 0.0  # Spot never goes short
        qty = floor_to_step(quantity, rules.market_step)
        cap = rules.market_max_qty or rules.max_qty
        if cap is not None:
            qty = min(qty, floor_to_step(cap, rules.market_step))
        if qty < max(rules.market_min_qty, rules.min_qty):
            return 0.0
        if rules.min_notional is not None and qty * entry_price < rules.min_notional:
            return 0.0
        # With headroom for a base-asset commission, what is left must itself
        # be protectable (pre-trade filter only; see SPOT_ENTRY_HEADROOM).
        worst_net = floor_to_step(qty * (1.0 - SPOT_ENTRY_HEADROOM), rules.step)
        if not self.can_protect(worst_net, stop_price):
            return 0.0
        return qty

    async def place_protection(self, *, side, quantity, stop_price, client_id, entry_price=None):
        if side != "long" or self.kind is None:
            raise ProtectionUnavailable("Spot protection is long-only")
        rules = self._require_ready()
        trigger = self.round_stop(stop_price, side="long")
        qty = floor_to_step(quantity, rules.step)
        if not self.can_protect(qty, trigger):
            raise ProtectionUnavailable("quantity cannot satisfy the symbol's filters")
        params: dict = {"stopLossPrice": trigger, "clientOrderId": client_id}
        if self.kind == KIND_SPOT_LIMIT:
            limit = self.limit_price_for(trigger)
            raw = await self.client.create_order(self.symbol, "limit", "sell", qty, limit, params)
        else:
            limit = None
            raw = await self.client.create_order(self.symbol, "market", "sell", qty, None, params)
        return self._protection_from(raw, client_id=client_id, stop_price=trigger, limit_price=limit)

    def _protection_from(self, raw, *, client_id, stop_price, limit_price, kind=None):
        info = order_from_ccxt(raw, base=self.base)
        if info.status == "open":
            state = "partial" if info.filled > 0 else "open"
        elif info.status == "closed":
            state = "filled"
        else:
            state = info.status
        return ProtectionOrder(
            client_id=client_id,
            order_id=info.order_id,
            kind=kind or self.kind or KIND_SPOT_MARKET,
            state=state,
            filled=info.filled,
            average=info.average,
            stop_price=stop_price,
            limit_price=limit_price,
        )

    async def get_protection(self, *, kind, client_id):
        try:
            raw = await self.client.fetch_order(client_id, self.symbol, {"clientOrderId": client_id})
        except ccxt_async.OrderNotFound:
            return None
        resolved_kind = kind or self.kind or KIND_SPOT_MARKET
        stop_price = _f(raw.get("stopPrice")) if isinstance(raw, dict) else None
        limit_price = _f(raw.get("price")) if (isinstance(raw, dict) and resolved_kind == KIND_SPOT_LIMIT) else None
        return self._protection_from(
            raw, client_id=client_id, stop_price=stop_price, limit_price=limit_price, kind=resolved_kind
        )

    async def list_open_bot_orders(self):
        orders = await self.client.fetch_open_orders(self.symbol)
        return [(str(o.get("clientOrderId")), False) for o in orders if _bot_owned(o.get("clientOrderId"))]

    async def place_exit(self, side, quantity, client_id):
        rules = self._require_ready()
        qty = floor_to_step(quantity, rules.market_step)
        if qty <= 0:
            raise ProtectionUnavailable("nothing to sell above the lot step")
        raw = await self.client.create_order(self.symbol, "market", "sell", qty, None, {"clientOrderId": client_id})
        return order_from_ccxt(raw, base=self.base)

    async def exposure(self) -> float:
        balance = await self.client.fetch_balance()
        entry = balance.get(self.base) if isinstance(balance, dict) else None
        if isinstance(entry, dict) and entry.get("free") is not None:
            return float(entry["free"])
        free = balance.get("free", {}) if isinstance(balance, dict) else {}
        return float((free or {}).get(self.base, 0.0) or 0.0)


# --- USD-M Futures ---------------------------------------------------------------


class FuturesVenue(Venue):
    market_type = "futures"

    @property
    def default_kind(self) -> str:
        return KIND_FUTURES_CLOSE

    async def _verify_capabilities(self, *, managing: bool = False) -> None:
        rules = self._require_ready()
        info = self.market.get("info") if isinstance(self.market.get("info"), dict) else {}
        status = info.get("status")
        if status is not None and str(status).upper() != "TRADING":
            raise ProtectionUnavailable("contract is not in TRADING status")
        if "STOP_MARKET" not in rules.order_types:
            raise ProtectionUnavailable("STOP_MARKET is not listed for this contract")
        await exchange.verify_futures_account_mode(
            self.client, self.symbol, max_age_seconds=MODE_CACHE_SECONDS if managing else 0
        )

    def plan_entry_quantity(self, quantity, *, entry_price, stop_price, side):
        rules = self._require_ready()
        qty = floor_to_step(quantity, rules.market_step)
        cap = rules.market_max_qty or rules.max_qty
        if cap is not None:
            qty = min(qty, floor_to_step(cap, rules.market_step))
        if qty < max(rules.market_min_qty, rules.min_qty):
            return 0.0
        if rules.min_notional is not None and qty * entry_price < rules.min_notional:
            return 0.0
        return qty

    # -- exchange truth ----------------------------------------------------

    async def signed_position(self) -> float:
        """Signed position size on this contract: > 0 long, < 0 short, 0.0
        flat. Raises if the answer cannot be read - an unreadable position
        is never treated as flat."""
        positions = await self.client.fetch_positions([self.symbol])
        total = 0.0
        for pos in positions or []:
            if not isinstance(pos, dict) or pos.get("symbol") not in (self.symbol, None):
                continue
            info = pos.get("info") if isinstance(pos.get("info"), dict) else {}
            amt = _f(info.get("positionAmt"))
            if amt is None:
                contracts = _f(pos.get("contracts"))
                if contracts is None:
                    raise ValueError("position size unreadable")
                amt = contracts if pos.get("side") != "short" else -contracts
            total += amt
        return total

    async def liquidation_price(self) -> float | None:
        positions = await self.client.fetch_positions([self.symbol])
        for pos in positions or []:
            if isinstance(pos, dict):
                value = _f(pos.get("liquidationPrice"))
                if value:
                    return value
        return None

    async def exposure(self) -> float:
        return abs(await self.signed_position())

    # -- protective stop ------------------------------------------------------

    @staticmethod
    def _stop_side(entry_side: str) -> str:
        return "sell" if entry_side == "long" else "buy"

    async def protection_precheck(self, *, side, stop_price):
        """A stop that sits beyond the liquidation price protects nothing:
        the position would be liquidated first. If Binance reports a
        liquidation price and the stop is not strictly on the safe side of
        it, refuse. (No reported liquidation price - common in cross
        margin with a large balance - does not block.)"""
        liq = await self.liquidation_price()
        if liq is None:
            return
        if side == "long" and stop_price <= liq:
            raise ProtectionUnavailable("stop is at or beyond the liquidation price")
        if side == "short" and stop_price >= liq:
            raise ProtectionUnavailable("stop is at or beyond the liquidation price")

    async def place_protection(self, *, side, quantity, stop_price, client_id, entry_price=None):
        trigger = self.round_stop(stop_price, side=side)
        base_params = {"stopLossPrice": trigger, "workingType": "MARK_PRICE", "clientOrderId": client_id}
        stop_side = self._stop_side(side)
        # Preferred: closePosition - sized by Binance to the whole position
        # at trigger time, cannot increase or reverse it, and needs no
        # quantity that could drift from reality after a partial fill.
        try:
            raw = await self.client.create_order(
                self.symbol, "market", stop_side, None, None, {**base_params, "closePosition": True}
            )
            kind = KIND_FUTURES_CLOSE
        except (ccxt_async.InvalidOrder, ccxt_async.BadRequest) as exc:
            logger.warning("closePosition protective stop rejected (%s); trying reduceOnly", type(exc).__name__)
            # Fallback: reduceOnly with the EXCHANGE-CONFIRMED position size,
            # never the number we think we hold. reduceOnly cannot grow or
            # flip a position.
            position = abs(await self.signed_position())
            qty = floor_to_step(position, self._require_ready().market_step)
            if qty <= 0:
                raise ProtectionUnavailable("no position on the exchange to protect") from exc
            raw = await self.client.create_order(
                self.symbol, "market", stop_side, qty, None, {**base_params, "reduceOnly": True}
            )
            kind = KIND_FUTURES_REDUCE
        return self._protection_from(raw, client_id=client_id, kind=kind, stop_price=trigger)

    def _protection_from(self, raw, *, client_id, kind, stop_price=None):
        if not isinstance(raw, dict):
            raise ValueError("unexpected conditional order shape")
        status = str(raw.get("status"))
        if status == "closed":
            state = "triggered"
        else:
            state = status
        oid = raw.get("id")
        return ProtectionOrder(
            client_id=client_id,
            order_id=str(oid) if oid is not None else None,
            kind=kind,
            state=state,
            filled=_f(raw.get("filled")) or 0.0,
            average=_f(raw.get("average")),
            stop_price=stop_price if stop_price is not None else _f(raw.get("stopPrice") or raw.get("triggerPrice")),
            limit_price=None,
        )

    async def get_protection(self, *, kind, client_id):
        kind = kind or KIND_FUTURES_CLOSE
        params = {"trigger": True, "clientAlgoId": client_id}
        try:
            raw = await self.client.fetch_order(client_id, self.symbol, params)
        except (ccxt_async.OrderNotFound, ccxt_async.BadRequest):
            raw = None
        if raw is not None:
            return self._protection_from(raw, client_id=client_id, kind=kind)
        # A direct lookup that finds nothing is not proof the stop is gone
        # (Binance answers unknown OR completed algo ids with a generic
        # error): also scan the open conditional orders.
        open_orders = await self.client.fetch_open_orders(self.symbol, None, None, {"trigger": True})
        for entry in open_orders or []:
            if isinstance(entry, dict) and entry.get("clientOrderId") == client_id:
                return self._protection_from(entry, client_id=client_id, kind=kind)
        return None

    async def list_open_bot_orders(self):
        found: list[tuple[str, bool]] = []
        conditional = await self.client.fetch_open_orders(self.symbol, None, None, {"trigger": True})
        found.extend((str(o.get("clientOrderId")), True) for o in conditional or [] if _bot_owned(o.get("clientOrderId")))
        regular = await self.client.fetch_open_orders(self.symbol)
        found.extend((str(o.get("clientOrderId")), False) for o in regular or [] if _bot_owned(o.get("clientOrderId")))
        return found

    # -- exit ----------------------------------------------------------------

    async def place_exit(self, side, quantity, client_id):
        qty = floor_to_step(quantity, self._require_ready().market_step)
        if qty <= 0:
            raise ProtectionUnavailable("nothing to close above the lot step")
        raw = await self.client.create_order(
            self.symbol,
            "market",
            self._stop_side(side),
            qty,
            None,
            {"reduceOnly": True, "clientOrderId": client_id},
        )
        return order_from_ccxt(raw, base=self.base)

    async def realized_exit(self, *, side: str, since_ms: int, max_quantity: float) -> tuple[float, float] | None:
        """(quantity, vwap) of exit-side fills since `since_ms`, capped at
        `max_quantity`, from the account's trade history; None if there are
        none. Used to price a close that happened on the exchange (stop
        triggered) where the conditional order itself carries no fills.

        Newest fills are taken first, up to the trade's own quantity, so a
        slightly early `since` (clock skew between this host and Binance)
        cannot pull in the previous trade's exit once this trade's own exit
        already accounts for its full quantity."""
        trades = await self.client.fetch_my_trades(self.symbol, since_ms)
        exit_side = self._stop_side(side)
        fills = [t for t in (trades or []) if isinstance(t, dict) and t.get("side") == exit_side]
        fills.sort(key=lambda t: t.get("timestamp") or 0, reverse=True)
        qty = 0.0
        notional = 0.0
        for t in fills:
            amount = _f(t.get("amount")) or 0.0
            price = _f(t.get("price"))
            if amount <= 0 or not price:
                continue
            take = min(amount, max_quantity - qty)
            if take <= 0:
                break
            qty += take
            notional += take * price
        if qty <= 0:
            return None
        return qty, notional / qty


def build_venue(
    client, *, market_type: str, pair: str, spot_stop_limit_buffer_pct: float = 0.5, entries_allowed: bool = False
) -> Venue:
    """Risk-reducing only unless `entries_allowed=True`, which only the
    new-entry path passes, after exchange.assert_trade_only."""
    if market_type == "futures":
        return FuturesVenue(client, pair, entries_allowed=entries_allowed)
    return SpotVenue(client, pair, stop_limit_buffer_pct=spot_stop_limit_buffer_pct, entries_allowed=entries_allowed)
