"""pair_catalog: listing and capital-based availability of Futures markets.

Pins what the capital rule does and, as importantly, what it refuses to claim: it is a NECESSARY
condition (the market's minimum vs capital * the bot's own notional ceiling), so it never marks
unavailable a market the bot could trade, and it never produces a dollar figure."""
from __future__ import annotations

import asyncio
import copy

import pytest
from fake_binance import FakeBinance

from mba_bot import exchange, pair_catalog
from mba_bot.config import ALL_KNOWN_PAIRS
from mba_bot.pair_catalog import (
    REASON_INSUFFICIENT_CAPITAL,
    REASON_NOT_LISTED,
    PairFacts,
    assess_pair,
    capital_supports_market,
    facts_from_market,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    pair_catalog.reset_cache()
    yield
    pair_catalog.reset_cache()


def _futures_market(min_notional=5.0, pair="BTC/USDT"):
    client = FakeBinance(market_type="futures", pair=pair, min_notional=min_notional)
    return asyncio.run(client.load_markets())[client.symbol]


# ------------------------------------------------------------------ facts from a market


def test_a_listed_perpetual_yields_its_minimum_notional():
    assert facts_from_market(_futures_market(100.0)) == PairFacts(listed=True, min_notional=100.0)


@pytest.mark.parametrize("breaker", ["inactive", "spot", "not_linear"])
def test_a_market_that_is_not_an_active_linear_perpetual_is_not_listed(breaker):
    market = copy.deepcopy(_futures_market())
    if breaker == "inactive":
        market["active"] = False
    elif breaker == "spot":
        market.update(spot=True, swap=False, contract=False)
    else:
        market["linear"] = False
    assert facts_from_market(market) == PairFacts(listed=False, min_notional=None)


def test_a_missing_market_is_not_listed():
    assert facts_from_market(None) == PairFacts(listed=False, min_notional=None)


def test_no_notional_filter_means_unknown_not_zero():
    assert facts_from_market(_futures_market(min_notional=None)) == PairFacts(listed=True, min_notional=None)


def test_unreadable_filters_mean_unknown_but_still_listed():
    market = copy.deepcopy(_futures_market())
    market["info"]["filters"] = []
    assert facts_from_market(market) == PairFacts(listed=True, min_notional=None)


# ------------------------------------------------------------------ the capital rule


@pytest.mark.parametrize(
    "capital, minimum, expected",
    [(20, 100, False), (33.0, 100, False), (33.34, 100, True), (50, 100, True), (1000, 100, True), (1, 5, False), (2, 5, True), (50, None, None)],
)
def test_capital_supports_market_is_the_bots_own_ceiling_against_the_exchange_minimum(capital, minimum, expected):
    assert capital_supports_market(capital, minimum, 3.0) is expected


def test_the_rule_uses_the_configured_ceiling_not_a_hardcoded_one():
    assert capital_supports_market(40, 100, 3.0) is True
    assert capital_supports_market(40, 100, 2.5) is True  # 40 * 2.5 = 100: exactly reachable
    assert capital_supports_market(40, 100, 2.4) is False


# ------------------------------------------------------------------ assessing one pair


def test_assess_pair_reasons_are_exact_and_carry_no_dollar_figure():
    low = assess_pair("BTC/USDT", PairFacts(True, 100.0), allocated_capital=20.0, max_notional_multiplier=3.0)
    assert (low.available, low.reason) == (False, "Insufficient allocated capital for this market.")
    gone = assess_pair("SUI/USDT", PairFacts(False, None), allocated_capital=1000.0, max_notional_multiplier=3.0)
    assert (gone.available, gone.reason) == (False, "Not available on Binance Futures right now.")
    for reason in (REASON_INSUFFICIENT_CAPITAL, REASON_NOT_LISTED):
        assert "$" not in reason and not any(ch.isdigit() for ch in reason), "no estimated minimum is ever displayed"


def test_a_market_is_available_whenever_a_check_cannot_be_made():
    assert assess_pair("BTC/USDT", None, allocated_capital=1.0, max_notional_multiplier=3.0).available, "no exchange data"
    assert assess_pair("BTC/USDT", PairFacts(True, 100.0), allocated_capital=None, max_notional_multiplier=3.0).available, "no capital set"
    assert assess_pair("BTC/USDT", PairFacts(True, None), allocated_capital=1.0, max_notional_multiplier=3.0).available, "minimum unknown"


def test_not_listed_takes_precedence_over_capital():
    st = assess_pair("BTC/USDT", PairFacts(False, 100.0), allocated_capital=1.0, max_notional_multiplier=3.0)
    assert st.reason == REASON_NOT_LISTED


# ------------------------------------------------------------------ the cached public read


class _CatalogClient:
    def __init__(self, notionals, fail=False):
        self.notionals, self.fail = notionals, fail
        self.loads = 0
        self.closed = False
        self.markets = None
        # Built here, not in load_markets: _futures_market uses asyncio.run,
        # which cannot be called from inside the running event loop.
        self._built = {
            f"{pair}:USDT": _futures_market(self.notionals.get(pair, 5.0), pair) for pair in ALL_KNOWN_PAIRS
        }

    async def load_markets(self):
        self.loads += 1
        if self.fail:
            raise RuntimeError("synthetic outage")
        self.markets = copy.deepcopy(self._built)

    async def close(self):
        self.closed = True


def _install(monkeypatch, *clients):
    queue = list(clients)
    built = []

    def build(**kw):
        assert kw["api_key"] == "" and kw["api_secret"] == "" and kw["sandbox"] is False, "public, Mainnet, no credentials"
        assert kw["market_type"] == "futures"
        client = queue.pop(0)
        built.append(client)
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", build)
    return built


def test_facts_cover_every_known_pair_and_are_cached(monkeypatch):
    client = _CatalogClient({"BTC/USDT": 100.0, "ETH/USDT": 20.0})
    built = _install(monkeypatch, client)

    first = asyncio.run(pair_catalog.futures_pair_facts())
    second = asyncio.run(pair_catalog.futures_pair_facts())

    assert set(first) == set(ALL_KNOWN_PAIRS) and first is second
    assert first["BTC/USDT"].min_notional == 100.0 and first["ETH/USDT"].min_notional == 20.0 and first["DOGE/USDT"].min_notional == 5.0
    assert client.loads == 1 and len(built) == 1 and client.closed


def test_the_cache_expires(monkeypatch):
    c1, c2 = _CatalogClient({"BTC/USDT": 100.0}), _CatalogClient({"BTC/USDT": 50.0})
    _install(monkeypatch, c1, c2)
    clock = {"t": 1000.0}
    monkeypatch.setattr(pair_catalog.time, "monotonic", lambda: clock["t"])
    assert asyncio.run(pair_catalog.futures_pair_facts())["BTC/USDT"].min_notional == 100.0
    clock["t"] += pair_catalog.CACHE_SECONDS + 1
    assert asyncio.run(pair_catalog.futures_pair_facts())["BTC/USDT"].min_notional == 50.0


def test_an_unreachable_exchange_never_raises_and_is_not_hammered(monkeypatch, caplog):
    broken = _CatalogClient({}, fail=True)
    built = _install(monkeypatch, broken)
    clock = {"t": 1000.0}
    monkeypatch.setattr(pair_catalog.time, "monotonic", lambda: clock["t"])

    assert asyncio.run(pair_catalog.futures_pair_facts()) is None
    assert asyncio.run(pair_catalog.futures_pair_facts()) is None, "answered from the failure window, no second attempt"
    assert broken.loads == 1 and broken.closed
    assert "synthetic outage" not in caplog.text, "only the failure category is logged"


def test_after_a_failure_the_last_good_answer_keeps_being_served(monkeypatch):
    good, bad = _CatalogClient({"BTC/USDT": 100.0}), _CatalogClient({}, fail=True)
    _install(monkeypatch, good, bad)
    clock = {"t": 1000.0}
    monkeypatch.setattr(pair_catalog.time, "monotonic", lambda: clock["t"])
    first = asyncio.run(pair_catalog.futures_pair_facts())
    clock["t"] += pair_catalog.CACHE_SECONDS + 1  # expired: the refresh fails
    assert asyncio.run(pair_catalog.futures_pair_facts()) == first, "stale facts beat no facts"


def test_a_symbol_missing_from_the_exchange_is_reported_not_listed(monkeypatch):
    client = _CatalogClient({})
    original = client.load_markets

    async def load():
        await original()
        client.markets.pop("SUI/USDT:USDT")

    client.load_markets = load
    _install(monkeypatch, client)
    facts = asyncio.run(pair_catalog.futures_pair_facts())
    assert facts["SUI/USDT"].listed is False and facts["DOGE/USDT"].listed is True
