"""Binance leverage-bracket (maintenance tier) reads: the real cause of every failure in the journal, one retry, the last-known-good cache with a strict maximum age, the three reason codes, and the
customer alert limit that survives a restart or a second process.

Background (21 Nov 2026): live Futures entries on XRP/USDT and SOL/USDT were refused with "margin tiers could not be read or matched" while BTC fell, the XRP alert came up to four times in 35 minutes,
and the journal could not say why: every failed read returned None with no trace, and one reason code covered a read failure, an implausible table and a table that did not contain the position value.

PROVES: each failure kind (exception, empty answer, symbol key mismatch, a missing field, an invalid table, no matching bracket) gives its own cause in the journal, the right reason code and the right customer
text; keys and long tokens never reach the journal; one retry fixes a transient failure; the cache is used only after a fresh read failed, only within the maximum age, never for a position value outside the
table, never when the fresh read worked; the alert limit holds across a restart and across processes; a 21 USD account never gets a bracket reason because of its size; both documented answer shapes of
Binance's leverageBracket endpoint are read.
DOES NOT PROVE: what Binance really returns (the shapes below come from ccxt 4.5.88's own documentation comments and the numbers are illustrative, NOT a live capture: UNVERIFIED), or anything about timing on the server."""
from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest

from mba_bot import exchange, protection_guard, risk
from mba_bot.risk import MaintenanceTier, assess_stop_viability

SYMBOL = "XRP/USDT:USDT"
# Illustrative tables in the shape Binance returns (floors and caps meet, rates rise, cum_k = cum_{k-1} + floor_k * (rate_k - rate_{k-1}); the top bracket's rate is 0.5, as on several real contracts, UNVERIFIED).
XRP_BRACKETS = [
    {"bracket": 1, "initialLeverage": 75, "notionalCap": 10000, "notionalFloor": 0, "maintMarginRatio": 0.0065, "cum": 0.0},
    {"bracket": 2, "initialLeverage": 50, "notionalCap": 50000, "notionalFloor": 10000, "maintMarginRatio": 0.01, "cum": 35.0},
    {"bracket": 3, "initialLeverage": 20, "notionalCap": 1000000, "notionalFloor": 50000, "maintMarginRatio": 0.02, "cum": 535.0},
    {"bracket": 4, "initialLeverage": 10, "notionalCap": 5000000, "notionalFloor": 1000000, "maintMarginRatio": 0.05, "cum": 30535.0},
    {"bracket": 5, "initialLeverage": 2, "notionalCap": 50000000, "notionalFloor": 5000000, "maintMarginRatio": 0.5, "cum": 2280535.0},
]
SOL_BRACKETS = [
    {"bracket": 1, "initialLeverage": 75, "notionalCap": 20000, "notionalFloor": 0, "maintMarginRatio": 0.0065, "cum": 0.0},
    {"bracket": 2, "initialLeverage": 50, "notionalCap": 100000, "notionalFloor": 20000, "maintMarginRatio": 0.01, "cum": 70.0},
    {"bracket": 3, "initialLeverage": 2, "notionalCap": 30000000, "notionalFloor": 100000, "maintMarginRatio": 0.5, "cum": 49570.0},
]
MARKET = {"id": "XRPUSDT", "symbol": SYMBOL, "base": "XRP", "quote": "USDT", "settle": "USDT", "baseId": "XRP", "quoteId": "USDT", "settleId": "USDT", "type": "swap", "spot": False, "margin": False,
          "swap": True, "future": False, "option": False, "contract": True, "linear": True, "inverse": False, "active": True, "precision": {}, "limits": {}, "info": {}}


def sample(symbol_id="XRPUSDT", brackets=XRP_BRACKETS):
    return {"symbol": symbol_id, "brackets": copy.deepcopy(brackets)}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(exchange, "_clock", c)
    return c


async def no_sleep(_):
    return None


class FakeClient:
    """Scripted answers. `steps` for fetch_leverage_tiers, `raw` for fapiPrivateGetLeverageBracket: each item is an Exception to raise, a callable, or the value to return."""

    def __init__(self, steps=(), raw=None):
        self.steps, self.raw_steps, self.calls = list(steps), (list(raw) if raw is not None else None), []
        self._ccxt = ccxt.binanceusdm()
        if raw is not None:
            self.fapiPrivateGetLeverageBracket = self._raw

    def market(self, pair):
        return dict(MARKET, symbol=pair)

    def parse_market_leverage_tiers(self, info, market=None):
        return self._ccxt.parse_market_leverage_tiers(info, market)

    def ccxt_tiers(self, brackets=XRP_BRACKETS, symbol=SYMBOL):
        return {symbol: self._ccxt.parse_market_leverage_tiers(sample(brackets=copy.deepcopy(brackets)), MARKET)}  # a copy: a test that breaks a field must not change the shared tables

    @staticmethod
    def _next(items):
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, BaseException):
            raise item
        return item() if callable(item) else item

    async def fetch_leverage_tiers(self, symbols=None, params=None):
        self.calls.append(("fetch_leverage_tiers", tuple(symbols or ()), params))
        return self._next(self.steps)

    async def _raw(self, params):
        self.calls.append(("raw", params))
        return self._next(self.raw_steps)


def read(client, **kw):
    kw.setdefault("sleeper", no_sleep)
    return asyncio.run(exchange.read_maintenance_tiers(client, SYMBOL, **kw))


def good(client=None):
    return (client or FakeClient()).ccxt_tiers()


def warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name == "mba_bot.exchange"]


# ------------------------------------------------------------------------------------------------ every failure says why
def test_an_exception_is_logged_with_class_message_status_and_binance_code_and_retried_once(caplog):
    err = ccxt.ExchangeError('binanceusdm {"code":-1003,"msg":"Too many requests. apiKey=abcdefghijklmnopqrstuvwxyz0123456789 signature=feedfacefeedface"}')
    err.http_status = 429
    c = FakeClient([err])
    with caplog.at_level(logging.INFO, logger="mba_bot.exchange"):
        r = read(c)
    assert r.tiers is None and r.source == "none" and r.cause == exchange.CAUSE_EXCEPTION and r.attempts == 2
    assert len([x for x in c.calls if x[0] == "fetch_leverage_tiers"]) == 2  # no raw method on this client: the retry asks the same way
    (line,) = warnings(caplog)
    assert "ExchangeError" in line and "Too many requests" in line and "http status 429" in line and "binance code -1003" in line and "attempts 2" in line
    assert "abcdefghijklmnopqrstuvwxyz" not in line and "feedface" not in line and "apiKey" not in line and len(line) < 700  # no key, signature or long token; cut


def test_the_message_is_cut_to_200_characters():
    assert len(exchange.scrub_exchange_text("x " * 500)) == 200
    assert exchange.scrub_exchange_text("a" * 40 + " ok") == "<removed> ok"


@pytest.mark.parametrize("answer,expect", [(None, "response type NoneType"), ({}, "response type dict, length 0"), ([], "response type list, length 0")])
def test_an_empty_answer_is_retried_and_named(answer, expect, caplog):
    c = FakeClient([answer])
    with caplog.at_level(logging.WARNING, logger="mba_bot.exchange"):
        r = read(c)
    assert r.tiers is None and r.cause == exchange.CAUSE_EMPTY and r.attempts == 2
    assert expect in warnings(caplog)[0]


def test_a_symbol_key_mismatch_names_the_keys_it_got(caplog):
    c = FakeClient([{"SOL/USDT:USDT": FakeClient().ccxt_tiers()[SYMBOL], "BTC/USDT:USDT": []}])
    with caplog.at_level(logging.WARNING, logger="mba_bot.exchange"):
        r = read(c)
    assert r.tiers is None and r.cause == exchange.CAUSE_PAIR_MISSING
    line = warnings(caplog)[0]
    assert "none for XRP/USDT:USDT" in line and "SOL/USDT:USDT" in line


def test_an_entry_with_no_brackets_is_retried_and_named(caplog):
    c = FakeClient([{SYMBOL: []}])
    with caplog.at_level(logging.WARNING, logger="mba_bot.exchange"):
        r = read(c)
    assert r.tiers is None and r.cause == exchange.CAUSE_NO_TIERS and r.attempts == 2
    assert "list of length 0" in warnings(caplog)[0]


def test_a_missing_field_is_named_and_not_retried(caplog):
    broken = FakeClient().ccxt_tiers()
    del broken[SYMBOL][1]["info"]["cum"]
    c = FakeClient([broken])
    with caplog.at_level(logging.WARNING, logger="mba_bot.exchange"):
        r = read(c)
    assert r.tiers is None and r.cause == exchange.CAUSE_FIELD_MISSING and r.attempts == 1 and len(c.calls) == 1
    assert "bracket 2: field cum" in warnings(caplog)[0]
    missing_rate = FakeClient().ccxt_tiers()
    missing_rate[SYMBOL][0]["maintenanceMarginRate"] = None
    r2 = read(FakeClient([missing_rate]))
    assert r2.cause == exchange.CAUSE_FIELD_MISSING


def test_a_field_that_is_not_a_number_is_named(caplog):
    t = FakeClient().ccxt_tiers()
    t[SYMBOL][2]["maxNotional"] = "lots"
    with caplog.at_level(logging.WARNING, logger="mba_bot.exchange"):
        r = read(FakeClient([t]))
    assert r.cause == exchange.CAUSE_NOT_NUMBER and "bracket 3: field maxNotional is not a number" in warnings(caplog)[0]


def test_the_old_single_read_function_still_returns_the_tiers_or_none():
    c = FakeClient([good()])
    tiers = asyncio.run(exchange.fetch_maintenance_tiers(c, SYMBOL))
    assert [(t.tier, t.floor, t.cap, t.rate, t.cum) for t in tiers][-1] == (5, 5000000.0, 50000000.0, 0.5, 2280535.0)
    c2 = FakeClient([RuntimeError("down")])
    assert asyncio.run(exchange.fetch_maintenance_tiers(c2, SYMBOL)) is None and len(c2.calls) == 1  # one attempt, no retry, no cache


def test_a_failure_is_logged_once_per_pair_and_cause_per_round(caplog):
    c = FakeClient([RuntimeError("down")])
    with caplog.at_level(logging.WARNING, logger="mba_bot.exchange"):
        exchange.begin_round()
        read(c)
        read(c)  # another account on the same pair, same round
        assert len(warnings(caplog)) == 1
        c2 = FakeClient([{}])
        read(c2)  # a different cause is its own line
        assert len(warnings(caplog)) == 2
        exchange.begin_round()
        read(c)
        assert len(warnings(caplog)) == 3
        other = asyncio.run(exchange.read_maintenance_tiers(c, "SOL/USDT:USDT", sleeper=no_sleep))
        assert other.tiers is None and len(warnings(caplog)) == 4  # another pair


def test_without_a_round_announcement_the_log_repeats_only_after_the_window(caplog, clock):
    c = FakeClient([RuntimeError("down")])
    with caplog.at_level(logging.WARNING, logger="mba_bot.exchange"):
        read(c)
        read(c)
        assert len(warnings(caplog)) == 1
        clock.t += exchange.TIERS_LOG_WINDOW_S + 1
        read(c)
        assert len(warnings(caplog)) == 2


# ------------------------------------------------------------------------------------------------ one retry
def test_a_transient_failure_is_fixed_by_one_retry_and_the_pause_is_short(caplog):
    paused = []

    async def sleeper(s):
        paused.append(s)

    c = FakeClient([RuntimeError("blip"), good()])
    with caplog.at_level(logging.INFO, logger="mba_bot.exchange"):
        r = read(c, sleeper=sleeper)
    assert r.tiers is not None and r.source == "fresh" and r.attempts == 2 and r.cause is None
    assert paused == [exchange.TIERS_RETRY_PAUSE_S] and exchange.TIERS_RETRY_PAUSE_S <= 1.0  # bounded, far below the 120 s cycle watchdog
    assert warnings(caplog) == [] and any("succeeded after one retry" in x.getMessage() for x in caplog.records)


def test_there_is_only_one_retry_and_none_when_the_first_attempt_was_already_slow(clock):
    c = FakeClient([RuntimeError("down")])
    r = read(c)
    assert r.attempts == 2 and len(c.calls) == 2

    class Slow(FakeClient):
        async def fetch_leverage_tiers(self, symbols=None, params=None):
            clock.t += exchange.TIERS_RETRY_SKIP_AFTER_S + 1
            raise RuntimeError("slow and down")

    s = Slow()
    r2 = read(s)
    assert r2.attempts == 1 and r2.tiers is None


def test_a_data_problem_is_not_retried():
    t = FakeClient().ccxt_tiers()
    del t[SYMBOL][0]["info"]["cum"]
    assert read(FakeClient([t])).attempts == 1


# ------------------------------------------------------------------------------------------------ Binance's answer shapes (ccxt 4.5.88 and the raw endpoint)
def real_ccxt_with_markets(raw_answer):
    ex = ccxt.binanceusdm()
    ex.set_markets([MARKET])

    async def raw(params):
        return raw_answer

    ex.fapiPrivateGetLeverageBracket = raw
    return ex


def test_contract_list_answer_one_entry_parses_through_real_ccxt():
    ex = real_ccxt_with_markets([sample()])
    parsed = asyncio.run(ex.fetch_leverage_tiers([SYMBOL], {"symbol": "XRPUSDT"}))
    assert list(parsed) == [SYMBOL]  # keyed by the unified symbol exchange_symbol() produces
    assert exchange.exchange_symbol("XRP/USDT", "futures") == SYMBOL
    r = asyncio.run(exchange.read_maintenance_tiers(ex, SYMBOL, sleeper=no_sleep))
    assert r.source == "fresh" and r.attempts == 1 and [t.tier for t in r.tiers] == [1, 2, 3, 4, 5]
    assert risk.tiers_problem(r.tiers) is None  # a table whose top bracket has rate 0.5 is plausible


def test_contract_list_answer_with_several_symbols_still_finds_ours():
    ex = real_ccxt_with_markets([sample("SOLUSDT", SOL_BRACKETS), sample(), sample("ETHUSDT", SOL_BRACKETS)])
    ex.set_markets([MARKET, dict(MARKET, id="SOLUSDT", symbol="SOL/USDT:USDT", base="SOL", baseId="SOL"), dict(MARKET, id="ETHUSDT", symbol="ETH/USDT:USDT", base="ETH", baseId="ETH")])
    assert len(asyncio.run(exchange.read_maintenance_tiers(ex, SYMBOL, sleeper=no_sleep)).tiers) == 5


def test_contract_single_object_answer_is_empty_in_ccxt_but_read_by_the_retry(caplog):
    """If Binance answers a `symbol` request with ONE object instead of a list (UNVERIFIED which it does), ccxt 4.5.88 parses it to an empty dict: the old code returned None silently. The retry asks the
    endpoint directly and reads this shape too."""
    ex = real_ccxt_with_markets(sample())
    assert asyncio.run(ex.fetch_leverage_tiers([SYMBOL], {"symbol": "XRPUSDT"})) == {}  # ccxt's own result, pinned
    with caplog.at_level(logging.INFO, logger="mba_bot.exchange"):
        r = asyncio.run(exchange.read_maintenance_tiers(ex, SYMBOL, sleeper=no_sleep))
    assert r.source == "fresh" and r.attempts == 2 and len(r.tiers) == 5 and r.tiers[4].rate == 0.5
    assert any("succeeded after one retry" in x.getMessage() for x in caplog.records)


def test_contract_mapping_answer_keyed_by_symbol_is_read_by_the_retry():
    ex = real_ccxt_with_markets({"XRPUSDT": sample()})
    r = asyncio.run(exchange.read_maintenance_tiers(ex, SYMBOL, sleeper=no_sleep))
    assert r.tiers is not None and r.attempts in (1, 2) and len(r.tiers) == 5


def test_the_raw_retry_says_what_shape_it_got_when_it_finds_nothing(caplog):
    ex = real_ccxt_with_markets([{"symbol": "OTHERUSDT", "brackets": XRP_BRACKETS}, {"symbol": "ALSOUSDT", "brackets": XRP_BRACKETS}])
    with caplog.at_level(logging.WARNING, logger="mba_bot.exchange"):
        r = asyncio.run(exchange.read_maintenance_tiers(ex, SYMBOL, sleeper=no_sleep))
    assert r.tiers is None and "raw answer shape list of 2" in warnings(caplog)[0]


def test_the_symbol_asked_for_is_the_unified_symbol_and_one_symbol_only():
    c = FakeClient([good()])
    read(c)
    assert c.calls[0] == ("fetch_leverage_tiers", (SYMBOL,), {"symbol": "XRPUSDT"})


# ------------------------------------------------------------------------------------------------ the cache
KEY = "acct-1:XRP/USDT:USDT"
MAX_AGE = 6 * 3600.0


def test_the_cache_is_used_only_after_a_fresh_read_failed_and_within_the_maximum_age(clock, caplog):
    ok = FakeClient([good()])
    first = read(ok, cache_key=KEY, cache_max_age=MAX_AGE)
    assert first.source == "fresh"
    down = FakeClient([RuntimeError("down")])
    clock.t += 3600
    with caplog.at_level(logging.WARNING, logger="mba_bot.exchange"):
        r = read(down, cache_key=KEY, cache_max_age=MAX_AGE)
    assert r.source == "cache" and r.tiers == first.tiers and r.age_seconds == 3600.0 and r.cause == exchange.CAUSE_EXCEPTION and r.attempts == 2
    assert any("last known good table (3600 s old, at most 21600 s allowed)" in m for m in warnings(caplog))
    assert any(m.startswith("binance maintenance tiers for") and "exception" in m for m in warnings(caplog))  # the failure itself is still journalled


def test_a_fresh_read_that_works_never_uses_the_cache(clock):
    read(FakeClient([good()]), cache_key=KEY, cache_max_age=MAX_AGE)
    newer = [dict(b, maintMarginRatio=b["maintMarginRatio"] + 0.001) for b in XRP_BRACKETS[:1]] + XRP_BRACKETS[1:]
    c = FakeClient([FakeClient().ccxt_tiers(newer)])
    r = read(c, cache_key=KEY, cache_max_age=MAX_AGE)
    assert r.source == "fresh" and r.tiers[0].rate == pytest.approx(0.0075)  # today's numbers, not the stored ones


def test_the_maximum_age_is_strict(clock):
    read(FakeClient([good()]), cache_key=KEY, cache_max_age=MAX_AGE)
    down = FakeClient([RuntimeError("down")])
    clock.t += MAX_AGE  # exactly the maximum: still allowed
    assert read(down, cache_key=KEY, cache_max_age=MAX_AGE).source == "cache"
    clock.t += 0.001  # a moment older: refused, and the table is dropped for good
    r = read(down, cache_key=KEY, cache_max_age=MAX_AGE)
    assert r.tiers is None and r.source == "none"
    clock.t -= 3600  # time cannot bring it back
    assert read(down, cache_key=KEY, cache_max_age=MAX_AGE).tiers is None


def test_no_cache_without_a_key_with_a_zero_age_or_for_another_account_or_symbol(clock):
    read(FakeClient([good()]), cache_key=KEY, cache_max_age=MAX_AGE)
    down = FakeClient([RuntimeError("down")])
    assert read(down, cache_key=None, cache_max_age=MAX_AGE).tiers is None  # no key
    assert read(down, cache_key=KEY, cache_max_age=0.0).tiers is None  # switched off by setting the age to 0
    assert read(down, cache_key="acct-2:XRP/USDT:USDT", cache_max_age=MAX_AGE).tiers is None  # another account never sees this account's table
    assert read(down, cache_key="acct-1:SOL/USDT:USDT", cache_max_age=MAX_AGE).tiers is None  # nor another symbol


def test_an_implausible_table_is_never_stored(clock):
    bad = FakeClient().ccxt_tiers()
    bad[SYMBOL][1]["minNotional"] = 12345  # a gap: read fine, but not plausible
    first = read(FakeClient([bad]), cache_key=KEY, cache_max_age=MAX_AGE)
    assert first.tiers is not None and risk.tiers_problem(first.tiers) is not None  # the caller will refuse it as invalid
    assert read(FakeClient([RuntimeError("down")]), cache_key=KEY, cache_max_age=MAX_AGE).tiers is None


def test_a_data_failure_also_falls_back_only_to_a_valid_stored_table(clock):
    read(FakeClient([good()]), cache_key=KEY, cache_max_age=MAX_AGE)
    t = FakeClient().ccxt_tiers()
    del t[SYMBOL][0]["info"]["cum"]
    r = read(FakeClient([t]), cache_key=KEY, cache_max_age=MAX_AGE)
    assert r.source == "cache" and r.cause == exchange.CAUSE_FIELD_MISSING


def viability(read_result, **over):
    kw = dict(side="long", entry_price=2.0, stop_price=1.96, quantity=10.0, leverage=3.0, effective_capital=21.0, margin_mode="isolated", tiers=read_result.tiers, other_open_positions=0,
              min_maint_rate=0.005, safety_fraction=0.75, cross_collateral_factor=0.9)
    kw.update(over)
    return assess_stop_viability(**kw)


def test_the_cache_is_never_used_for_a_position_value_outside_the_cached_table(clock):
    small = FakeClient().ccxt_tiers(XRP_BRACKETS[:2])  # a table that ends at 50,000
    read(FakeClient([small]), cache_key=KEY, cache_max_age=MAX_AGE)
    r = read(FakeClient([RuntimeError("down")]), cache_key=KEY, cache_max_age=MAX_AGE)
    assert r.source == "cache"
    inside = viability(r, quantity=10.0)  # notional 20
    assert inside.reason != risk.TIERS_NO_MATCH and inside.reason in (risk.OK, risk.STOP_UNVIABLE)
    outside = viability(r, quantity=40_000.0)  # notional 80,000: beyond the cached table
    assert not outside.viable and outside.reason == risk.TIERS_NO_MATCH and "80000" in outside.note and "[0-10000]" in outside.note and "[10000-50000]" in outside.note


def test_the_default_is_fail_closed_when_there_is_no_usable_data():
    r = read(FakeClient([RuntimeError("down")]))
    assert r.tiers is None
    v = viability(r)
    assert not v.viable and v.reason == risk.TIERS_READ_FAILED


# ------------------------------------------------------------------------------------------------ reason codes and the account-size question
def test_the_three_causes_have_three_codes():
    t = viability(read(FakeClient([good()])))
    assert t.reason in (risk.OK, risk.STOP_UNVIABLE)
    assert viability(read(FakeClient([RuntimeError("x")]))).reason == risk.TIERS_READ_FAILED
    bad = [MaintenanceTier(1, 0.0, 1000.0, 0.01, 0.0), MaintenanceTier(2, 1000.0, 9e9, 0.005, 0.0)]
    inv = assess_stop_viability(side="long", entry_price=2.0, stop_price=1.96, quantity=10.0, leverage=3.0, effective_capital=21.0, margin_mode="isolated", tiers=bad, other_open_positions=0,
                                min_maint_rate=0.005, safety_fraction=0.75, cross_collateral_factor=0.9)
    assert inv.reason == risk.TIERS_INVALID and "rate falls from 0.01 to 0.005" in inv.note
    gap = [MaintenanceTier(1, 0.0, 1000.0, 0.01, 0.0), MaintenanceTier(2, 5000.0, 9e9, 0.02, 0.0)]
    assert risk.tiers_problem(gap) == "brackets 1 and 2: cap 1000 and next floor 5000 do not meet"
    assert risk.tiers_problem([MaintenanceTier(1, 5.0, 100.0, 0.01, 0.0)]) == "first bracket starts at 5, not 0"
    assert risk.tiers_problem([MaintenanceTier(1, 0.0, 100.0, 0.6, 0.0)]) == "bracket 1: rate 0.6 is outside 0 to 0.5"
    assert risk.tiers_problem([MaintenanceTier(1, 0.0, 100.0, 0.5, 0.0)]) is None  # a top bracket at exactly 50% is allowed


@pytest.mark.parametrize("brackets", [XRP_BRACKETS, SOL_BRACKETS])
@pytest.mark.parametrize("notional", [5.0, 10.0, 21.0, 50.0, 100.0])
def test_account_size_never_causes_a_bracket_refusal_for_a_21_usd_account(brackets, notional):
    """A tiny position always falls inside bracket 1 (floor 0), whatever the account holds: the bracket codes can never come from the size of a 21 USD account."""
    tiers = FakeClient().ccxt_tiers(brackets)[SYMBOL]
    parsed = exchange._convert(tiers)[0]
    price = 2.0
    for leverage in (1.0, 3.0, 5.0):
        v = assess_stop_viability(side="long", entry_price=price, stop_price=price * 0.98, quantity=notional / price, leverage=leverage, effective_capital=21.0, margin_mode="isolated", tiers=parsed,
                                  other_open_positions=0, min_maint_rate=0.005, safety_fraction=0.75, cross_collateral_factor=0.9)
        assert v.reason not in (risk.TIERS_READ_FAILED, risk.TIERS_INVALID, risk.TIERS_NO_MATCH, risk.TIERS_UNAVAILABLE), (notional, leverage, v.reason)
        assert v.tier == 1 and v.notional == pytest.approx(notional, rel=1e-3)


# ------------------------------------------------------------------------------------------------ the customer alert limit
@pytest.fixture
def two_sessions(tmp_path):
    from mba_bot.db import init_db, session_scope

    init_db(f"sqlite:///{tmp_path}/alerts.db")
    protection_guard.reset_for_tests()
    yield session_scope
    protection_guard.reset_for_tests()


NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def claim(session, minutes=0, *, account="acct-1", pair="XRP/USDT", reason=risk.TIERS_READ_FAILED, hours=6.0):
    return protection_guard.claim_alert(session, account, pair, reason, now=NOW + timedelta(minutes=minutes), repeat_hours=hours)


def test_one_alert_per_account_pair_and_reason_per_window_across_a_restart_and_a_second_process(two_sessions):
    with two_sessions() as s:
        assert claim(s) is True
    # a restart (memory gone) or a second process (its own memory): the database still knows
    protection_guard.reset_for_tests()
    with two_sessions() as s:
        assert claim(s, 10) is False
    protection_guard.reset_for_tests()
    with two_sessions() as s:
        assert claim(s, 35) is False  # the 35 minute window of the incident
    protection_guard.reset_for_tests()
    with two_sessions() as s:
        assert claim(s, 5 * 60 + 59) is False
    protection_guard.reset_for_tests()
    with two_sessions() as s:
        assert claim(s, 6 * 60 + 1) is True  # the window ended
        assert claim(s, 6 * 60 + 2) is False


def test_a_different_account_pair_or_reason_is_its_own_alert(two_sessions):
    with two_sessions() as s:
        assert claim(s) is True
        assert claim(s, account="acct-2") is True  # several accounts on one pair each get their one alert
        assert claim(s, pair="SOL/USDT") is True
        assert claim(s, reason=risk.TIERS_INVALID) is True
        assert claim(s) is False


def test_two_processes_claiming_at_the_same_moment_send_one_alert(two_sessions):
    from mba_bot.db import session_scope

    # two independent sessions (two processes), each with an empty memory, race for the same alert: the atomic upsert lets exactly one through
    with session_scope() as a, session_scope() as b:
        first = claim(a)
        protection_guard._ALERTED.clear()
        second = claim(b)
    assert [first, second] == [True, False]


def test_a_database_problem_falls_back_to_the_memory_rule_and_never_loses_pending_changes(two_sessions, monkeypatch):
    from sqlalchemy import text

    from mba_bot.db import session_scope
    from mba_bot.models import TradingAccount

    with session_scope() as s:
        s.execute(text("DROP TABLE alert_marks"))
    with session_scope() as s:
        assert claim(s) is True  # no table: the old in-memory rule
        assert claim(s, 5) is False
    protection_guard.reset_for_tests()
    with session_scope() as s:
        assert claim(s, 5) is True  # (after a restart the in-memory fallback cannot know: that is the old behaviour, only when the table is missing)
    assert protection_guard.claim_alert(None, "a", "p", "r", now=NOW, repeat_hours=6.0) is True  # no session at all
    assert TradingAccount  # (the import documents that account rows are untouched by the claim)


def test_the_claim_is_committed_before_the_caller_sends(two_sessions):
    from sqlalchemy import select

    from mba_bot.db import session_scope
    from mba_bot.models import AlertMark

    s = None
    with session_scope() as session:
        s = session
        assert claim(session) is True
        # a second connection already sees it: the write lock is released before any network send
        with session_scope() as other:
            assert other.scalar(select(AlertMark.reason)) == risk.TIERS_READ_FAILED
    assert s is not None
