"""Which Futures markets a customer can choose, and why one may be unavailable.

Used only when `Settings.futures_pair_expansion` is on, and only for USD-M
Futures. Two independent reasons a market can be marked unavailable:

* NOT LISTED: Binance does not currently offer it as an active linear
  perpetual (checked with the same `exchange.verify_market` the entry path
  uses).
* INSUFFICIENT CAPITAL: the customer's allocated capital cannot reach the
  market's minimum order size even at the bot's own notional ceiling. This is
  a NECESSARY condition, computed from the exchange's CURRENT filters and the
  bot's configured ceiling (`allocated_capital * MAX_NOTIONAL_MULTIPLIER`
  against the market's minimum notional): it never marks unavailable a market
  the bot could trade, so it never wrongly blocks anyone. It is deliberately
  not an estimate of "the smallest capital that works" - that depends on
  volatility and the account's leverage, and no figure is shown until it can be
  computed reliably. The entry path keeps its own exact checks (filters,
  minimums, margin ceiling); nothing here replaces them.

If the exchange data cannot be read the answer is "unknown" and the market is
shown as available (fail-open for DISPLAY only; every real entry is still
checked in full).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace

from . import exchange
from .config import ALL_KNOWN_PAIRS
from .venue import ProtectionUnavailable, rules_from_market

logger = logging.getLogger(__name__)

REASON_INSUFFICIENT_CAPITAL = "Insufficient allocated capital for this market."
REASON_NOT_LISTED = "Not available on Binance Futures right now."

CACHE_SECONDS = 3600  # exchange filters change rarely
RETRY_AFTER_FAILURE_SECONDS = 120


@dataclass(frozen=True)
class PairFacts:
    listed: bool
    min_notional: float | None  # None = the exchange did not say / could not be read


@dataclass(frozen=True)
class PairStatus:
    pair: str
    available: bool
    reason: str | None


def facts_from_market(market) -> PairFacts:
    """`market` is a loaded ccxt market dict (or None if the symbol is absent)."""
    if not isinstance(market, dict):
        return PairFacts(listed=False, min_notional=None)
    try:
        exchange.verify_market(SimpleNamespace(markets={"x": market}), "x", market_type="futures")
    except exchange.UnsupportedSymbol:
        return PairFacts(listed=False, min_notional=None)
    try:
        min_notional = rules_from_market(market).min_notional
    except ProtectionUnavailable:
        min_notional = None
    return PairFacts(listed=True, min_notional=min_notional)


def capital_supports_market(allocated_capital: float, min_notional: float | None, max_notional_multiplier: float) -> bool | None:
    """True/False, or None when the market's minimum is unknown."""
    if min_notional is None:
        return None
    return allocated_capital * max_notional_multiplier >= min_notional


def assess_pair(pair: str, facts: PairFacts | None, *, allocated_capital: float | None, max_notional_multiplier: float) -> PairStatus:
    """`facts` None (catalog unavailable) or `allocated_capital` None (not
    configured yet) means that check cannot be made and the pair is shown available."""
    if facts is None:
        return PairStatus(pair, True, None)
    if not facts.listed:
        return PairStatus(pair, False, REASON_NOT_LISTED)
    if allocated_capital is not None and capital_supports_market(allocated_capital, facts.min_notional, max_notional_multiplier) is False:
        return PairStatus(pair, False, REASON_INSUFFICIENT_CAPITAL)
    return PairStatus(pair, True, None)


_cache: dict = {"facts": None, "at": 0.0, "failed_at": 0.0}


async def futures_pair_facts() -> dict[str, PairFacts] | None:
    """Current facts for every known pair from Binance's PUBLIC exchange
    information (no credentials, Mainnet), cached. None if never readable."""
    now = time.monotonic()
    if _cache["facts"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["facts"]
    if now - _cache["failed_at"] < RETRY_AFTER_FAILURE_SECONDS:
        return _cache["facts"]  # the last good answer (possibly stale) or None
    client = None
    try:
        client = exchange.build_exchange_client(exchange="binance", market_type="futures", api_key="", api_secret="", sandbox=False)
        await client.load_markets()
        markets = client.markets or {}
        facts = {pair: facts_from_market(markets.get(exchange.exchange_symbol(pair, "futures"))) for pair in ALL_KNOWN_PAIRS}
    except Exception as exc:
        _cache["failed_at"] = now
        logger.warning("pair catalog: could not read Binance Futures market info (%s); showing pairs without availability checks", type(exc).__name__)
        return _cache["facts"]
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
    _cache.update(facts=facts, at=now)
    return facts


def reset_cache() -> None:  # tests
    _cache.update(facts=None, at=0.0, failed_at=0.0)
