"""Pre-entry protection check (real Binance maintenance-margin brackets), the three guard modes, failure
classification and the repeat-failure limit.

Background: an account at 100x leverage on XRP opened and immediately emergency-closed the same kind of trade 23 times
in under two hours, because the 2x ATR stop sat beyond the liquidation price and Binance cannot hold a stop there.

These tests do NOT claim the liquidation formula is verified against an official Binance page; they pin what this
code does with the inputs it is given, and that every missing or implausible input fails closed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest
from fake_binance import FakeBinance
from helpers_live import *  # noqa: F401,F403

from mba_bot import config as config_mod
from mba_bot import exchange, lifecycle, protection_guard, risk, scheduler, telemetry
from mba_bot.config import guard_mode_is_unrecognised, parse_guard_mode
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, Trade, TradeTelemetry, TradingAccount
from mba_bot.risk import MaintenanceTier, assess_stop_viability, validate_tiers
from mba_bot.strategy import SignalType, candles_from_ohlcv, generate_signal
from mba_bot.venue import ProtectionUnavailable


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    telemetry.reset_for_tests()
    protection_guard.reset_for_tests()
    yield
    scheduler._account_locks.clear()
    scheduler._stragglers.clear()
    telemetry.reset_for_tests()
    protection_guard.reset_for_tests()


# A plausible multi-bracket table in the shape Binance's leverageBracket returns. The numbers are illustrative (they
# are continuous: cum_k = cum_{k-1} + floor_k * (rate_k - rate_{k-1})), NOT a copy of any live contract's table.
TIERS = [
    MaintenanceTier(1, 0.0, 50_000.0, 0.004, 0.0),
    MaintenanceTier(2, 50_000.0, 250_000.0, 0.005, 50.0),
    MaintenanceTier(3, 250_000.0, 1_000_000.0, 0.01, 1_300.0),
]
FLAT_TIER = [MaintenanceTier(1, 0.0, 1e9, 0.004, 0.0)]


def _check(**over):
    kw = dict(
        side="long", entry_price=100.0, stop_price=98.0, quantity=1.0, leverage=10.0, effective_capital=100.0,
        margin_mode="isolated", tiers=FLAT_TIER, other_open_positions=0, min_maint_rate=0.005, safety_fraction=0.75,
        cross_collateral_factor=0.9,
    )
    kw.update(over)
    return assess_stop_viability(**kw)


# ------------------------------------------------------------------ the calculation


def test_the_incident_100x_with_a_2_percent_stop_is_refused():
    r = _check(leverage=100.0, stop_price=98.0)
    assert not r.viable and r.reason == risk.STOP_UNVIABLE
    assert r.liquidation_distance_frac == pytest.approx(0.005)  # 1/100 - 0.5% effective rate
    assert r.max_allowed_stop_frac == pytest.approx(0.00375) and r.stop_distance_frac == pytest.approx(0.02)


def test_5x_with_a_1_percent_stop_is_allowed_like_the_protected_bnb_trades():
    r = _check(leverage=5.0, stop_price=99.0)
    assert r.viable and r.reason == risk.OK
    assert r.liquidation_distance_frac == pytest.approx(0.195)


@pytest.mark.parametrize(
    "leverage, stop_frac, viable",
    [
        (20.0, 0.03, True),      # allowed = 0.75 * (5% - 0.5%) = 3.375%
        (20.0, 0.03375, True),   # exactly the limit: equality passes
        (20.0, 0.0338, False),   # just past it
        (20.0, 0.0375, False),
        (50.0, 0.01, True),      # allowed = 0.75 * (2% - 0.5%) = 1.125%
        (50.0, 0.0113, False),
    ],
)
def test_boundaries_flat_tier(leverage, stop_frac, viable):
    stop = 100.0 * (1 - stop_frac)
    assert _check(leverage=leverage, stop_price=stop).viable is viable


def test_short_uses_the_short_denominator_and_is_otherwise_symmetric():
    long_r = _check(side="long", leverage=20.0, stop_price=97.0)
    short_r = _check(side="short", leverage=20.0, stop_price=103.0)
    assert short_r.liquidation_distance_frac == pytest.approx(long_r.liquidation_distance_frac / 1.004, abs=1e-5)
    assert not _check(side="short", leverage=100.0, stop_price=102.0).viable
    assert _check(side="short", leverage=5.0, stop_price=101.0).viable


def test_leverage_so_high_that_no_liquidation_distance_remains_is_refused():
    for lev in (100.0, 125.0):
        assert not _check(leverage=lev, stop_price=99.0).viable  # a 1% stop


# ---- maintenance brackets: tier selection and the maintenance amount (cum)


def test_the_bracket_for_the_order_notional_is_used():
    small = _check(tiers=TIERS, quantity=1.0)                         # notional 100       -> tier 1
    mid = _check(tiers=TIERS, quantity=1_000.0, effective_capital=1e9)  # notional 100,000  -> tier 2
    big = _check(tiers=TIERS, quantity=5_000.0, effective_capital=1e9)  # notional 500,000  -> tier 3
    assert (small.tier, mid.tier, big.tier) == (1, 2, 3)
    assert (small.tier_rate, mid.tier_rate, big.tier_rate) == (0.004, 0.005, 0.01)


def test_the_maintenance_amount_lowers_the_effective_rate():
    mid = _check(tiers=TIERS, quantity=1_000.0, effective_capital=1e9, min_maint_rate=0.0)
    assert mid.effective_maint_rate == pytest.approx(0.005 - 50.0 / 100_000.0)  # 0.45%, not the bracket's 0.5%
    big = _check(tiers=TIERS, quantity=5_000.0, effective_capital=1e9, min_maint_rate=0.0)
    assert big.effective_maint_rate == pytest.approx(0.01 - 1_300.0 / 500_000.0)  # 0.74%, not 1%
    # Ignoring cum would have used the full bracket rate and been over-conservative here; with cum it is exact.
    no_cum = [MaintenanceTier(t.tier, t.floor, t.cap, t.rate, 0.0) for t in TIERS]
    assert _check(tiers=no_cum, quantity=5_000.0, effective_capital=1e9, min_maint_rate=0.0).effective_maint_rate == pytest.approx(0.01)


def test_the_configured_floor_wins_when_the_bracket_rate_is_lower():
    r = _check(tiers=FLAT_TIER, min_maint_rate=0.02, leverage=20.0, stop_price=99.0)
    assert r.effective_maint_rate == pytest.approx(0.02)
    assert r.liquidation_distance_frac == pytest.approx(0.05 - 0.02)


def test_a_higher_bracket_rate_makes_a_previously_fine_stop_unviable():
    # 20x, 3% stop: fine at 0.4%, not at a 2.5% contract.
    assert _check(tiers=FLAT_TIER, leverage=20.0, stop_price=97.0).viable
    high = [MaintenanceTier(1, 0.0, 1e9, 0.025, 0.0)]
    assert not _check(tiers=high, leverage=20.0, stop_price=97.0).viable


def test_a_notional_exactly_on_a_bracket_boundary_takes_the_worse_bracket():
    r = _check(tiers=TIERS, quantity=500.0, effective_capital=1e9, min_maint_rate=0.0)  # notional 50,000 exactly
    assert r.notional == 50_000.0 and r.effective_maint_rate == pytest.approx(0.004)  # both give 0.4%: continuous
    discontinuous = [MaintenanceTier(1, 0.0, 50_000.0, 0.004, 0.0), MaintenanceTier(2, 50_000.0, 250_000.0, 0.01, 0.0)]
    r2 = _check(tiers=discontinuous, quantity=500.0, effective_capital=1e9, min_maint_rate=0.0)
    assert r2.tier == 2 and r2.effective_maint_rate == pytest.approx(0.01)


@pytest.mark.parametrize(
    "tiers",
    [
        None, [],
        [MaintenanceTier(1, 0.0, 1e9, 0.6, 0.0)],                                   # implausible rate
        [MaintenanceTier(1, 0.0, 1e9, -0.01, 0.0)],
        [MaintenanceTier(1, 0.0, 1e9, 0.004, -5.0)],                                # negative cum
        [MaintenanceTier(1, 0.0, 1e9, float("nan"), 0.0)],
        [MaintenanceTier(1, 0.0, 1e9, 0.004, float("inf"))],
        [MaintenanceTier(1, 100.0, 1e9, 0.004, 0.0)],                               # does not start at 0
        [MaintenanceTier(1, 0.0, 0.0, 0.004, 0.0)],                                 # empty range
        [MaintenanceTier(1, 0.0, 1000.0, 0.004, 0.0), MaintenanceTier(2, 5000.0, 1e9, 0.005, 0.0)],   # gap
        [MaintenanceTier(1, 0.0, 1000.0, 0.004, 0.0), MaintenanceTier(2, 500.0, 1e9, 0.005, 0.0)],    # overlap
        [MaintenanceTier(1, 0.0, 1000.0, 0.01, 0.0), MaintenanceTier(2, 1000.0, 1e9, 0.005, 0.0)],    # rate falls with size
        [MaintenanceTier(1, 0.0, 50.0, 0.004, 0.0)],                                # notional (100) above the last cap
        ["not a tier"],
    ],
)
def test_missing_invalid_or_unmatchable_brackets_fail_closed(tiers):
    r = _check(tiers=tiers)
    assert not r.viable and r.reason == risk.TIERS_UNAVAILABLE


def test_validate_tiers_accepts_a_real_looking_table_and_sorts_it():
    assert [t.tier for t in validate_tiers(list(reversed(TIERS)))] == [1, 2, 3]


# ---- margin mode and cross margin


@pytest.mark.parametrize("mode", [None, "", "portfolio", "CROSS", 1])
def test_an_unknown_margin_mode_is_refused_not_assumed(mode):
    r = _check(margin_mode=mode)
    assert not r.viable and r.reason == risk.MARGIN_MODE_UNKNOWN


def test_cross_margin_with_a_clean_account_uses_the_collateral():
    # 100x set, but 100 of collateral (x0.9) against a 100-notional position: liquidation is far away.
    r = _check(margin_mode="cross", leverage=100.0, quantity=1.0, effective_capital=100.0)
    assert r.viable and r.margin_basis == "cross"
    assert r.liquidation_distance_frac == pytest.approx(0.9 * 100 / 100 - 0.005)
    # Isolated with the same inputs is refused: only the margin mode differs.
    assert not _check(margin_mode="isolated", leverage=100.0, quantity=1.0, effective_capital=100.0).viable


def test_cross_margin_never_reports_less_than_the_isolated_estimate():
    iso = _check(margin_mode="isolated", leverage=5.0)
    cross = _check(margin_mode="cross", leverage=5.0)
    assert cross.liquidation_distance_frac >= iso.liquidation_distance_frac


@pytest.mark.parametrize("others", [1, 2, 7, None, True, -1, 0.5])
def test_cross_margin_is_refused_when_other_positions_exist_or_the_count_is_unreliable(others):
    r = _check(margin_mode="cross", other_open_positions=others, leverage=2.0, stop_price=99.9)
    assert not r.viable and r.reason == risk.CROSS_UNVERIFIABLE


def test_the_collateral_haircut_reduces_the_cross_estimate():
    full = _check(margin_mode="cross", leverage=125.0, quantity=1.0, effective_capital=10.0, cross_collateral_factor=1.0)
    cut = _check(margin_mode="cross", leverage=125.0, quantity=1.0, effective_capital=10.0, cross_collateral_factor=0.5)
    assert full.liquidation_distance_frac > cut.liquidation_distance_frac


# ---- leverage range and other invalid inputs


@pytest.mark.parametrize("lev", [0.0, 0.5, -5.0, 125.01, 1000.0, float("nan"), float("inf"), None, True, "10", "abc"])
def test_leverage_outside_1_to_125_or_not_a_number_is_invalid(lev):
    r = _check(leverage=lev)
    assert not r.viable and r.reason in (risk.INPUTS_INVALID,)


@pytest.mark.parametrize("lev", [1.0, 2.0, 125.0])
def test_leverage_bounds_are_inclusive(lev):
    assert risk.leverage_is_sane(lev)
    assert _check(leverage=lev, stop_price=99.99).reason in (risk.OK, risk.STOP_UNVIABLE)


@pytest.mark.parametrize(
    "over",
    [
        dict(entry_price=0.0), dict(entry_price=float("nan")), dict(entry_price=None), dict(stop_price=0.0), dict(stop_price=None),
        dict(quantity=0.0), dict(quantity=-1.0), dict(effective_capital=0.0), dict(effective_capital=float("inf")),
        dict(stop_price=101.0),               # long with the stop above entry
        dict(side="short", stop_price=99.0),  # short with the stop below entry
        dict(stop_price=100.0),               # no distance at all
        dict(side="sideways"), dict(min_maint_rate=0.6), dict(min_maint_rate=-0.1), dict(safety_fraction=0.0),
        dict(safety_fraction=1.5), dict(cross_collateral_factor=0.0), dict(cross_collateral_factor=2.0),
    ],
)
def test_missing_or_unreliable_numeric_inputs_fail_closed(over):
    r = _check(**over)
    assert not r.viable and r.reason == risk.INPUTS_INVALID


def test_detail_uses_fractions_and_contains_no_secrets():
    d = _check(leverage=100.0).detail()
    assert {"stop_distance_frac", "liquidation_distance_frac", "max_allowed_stop_frac", "tier", "effective_maint_rate"} <= set(d)
    assert "pct" not in " ".join(d)
    json.dumps(d)  # serialisable


# ------------------------------------------------------------------ realistic ccxt payloads


def _ccxt():
    return ccxt.binanceusdm()


# Shapes as documented in the pinned ccxt (4.5.88) sources for Binance's symbolConfig and leverageBracket endpoints.
SYMBOL_CONFIG = {"symbol": "XRPUSDT", "marginType": "CROSSED", "isAutoAddMargin": "false", "leverage": 100, "maxNotionalValue": "INF"}
BRACKETS = {
    "symbol": "XRPUSDT",
    "brackets": [
        {"bracket": 1, "initialLeverage": 75, "notionalCap": 10000, "notionalFloor": 0, "maintMarginRatio": 0.005, "cum": 0.0},
        {"bracket": 2, "initialLeverage": 50, "notionalCap": 50000, "notionalFloor": 10000, "maintMarginRatio": 0.01, "cum": 50.0},
        {"bracket": 3, "initialLeverage": 25, "notionalCap": 250000, "notionalFloor": 50000, "maintMarginRatio": 0.025, "cum": 800.0},
    ],
}


class _CcxtStub:
    """Feeds real ccxt parsing with Binance-shaped payloads, offline."""

    def __init__(self, config=None, brackets=None, positions=None, raises=()):
        self.ex = _ccxt()
        self.config, self.brackets, self.positions, self.raises = config, brackets, positions, raises
        self.tier_params = None

    def market(self, pair):
        return {"id": "XRPUSDT"}

    async def fetch_leverage(self, pair):
        if "leverage" in self.raises:
            raise ccxt.NetworkError("down")
        return self.ex.parse_leverage(self.config)

    async def fetch_leverage_tiers(self, symbols, params=None):
        self.tier_params = params
        if "tiers" in self.raises:
            raise ccxt.RequestTimeout("slow")
        return {symbols[0]: self.ex.parse_market_leverage_tiers(self.brackets)}

    async def fetch_positions(self, symbols=None):
        if "positions" in self.raises:
            raise ccxt.NetworkError("down")
        return self.positions


def test_real_ccxt_parsing_of_symbol_config_gives_leverage_and_margin_mode():
    import asyncio

    for raw_mode, expected in (("CROSSED", "cross"), ("ISOLATED", "isolated")):
        stub = _CcxtStub(config={**SYMBOL_CONFIG, "marginType": raw_mode})
        lev, mode = asyncio.run(exchange.fetch_symbol_leverage_and_margin_mode(stub, "XRP/USDT:USDT", side="long"))
        assert (lev, mode) == (100.0, expected)
    # Not reported -> unknown (and the check then refuses); a lowercase positionRisk-style word is read as isolated.
    no_mode = {k: v for k, v in SYMBOL_CONFIG.items() if k != "marginType"}
    assert asyncio.run(exchange.fetch_symbol_leverage_and_margin_mode(_CcxtStub(config=no_mode), "X", side="long")) == (100.0, None)
    assert asyncio.run(exchange.fetch_symbol_leverage_and_margin_mode(_CcxtStub(config={**SYMBOL_CONFIG, "marginType": "cross"}), "X", side="short"))[1] == "isolated"


def _read_leverage(value):
    import asyncio

    class Raw:
        async def fetch_leverage(self, pair):
            return {"marginMode": "isolated", "longLeverage": value, "shortLeverage": value}

    return asyncio.run(exchange.fetch_symbol_leverage_and_margin_mode(Raw(), "X", side="long"))[0]


@pytest.mark.parametrize("bad", [0, -3, True, False, None, "abc", "", float("nan"), float("inf"), [1], {"a": 1}])
def test_the_exchange_reader_rejects_values_that_are_not_a_leverage(bad):
    assert _read_leverage(bad) is None


@pytest.mark.parametrize("value, expected", [(1, 1.0), (125, 125.0), ("20", 20.0), (0.5, 0.5), (126, 126.0), (200, 200.0)])
def test_the_exchange_reader_does_not_apply_the_plausible_range(value, expected):
    """With the guards off the read behaves as before they existed: any positive finite number comes through. The
    1..max range is applied by the pre-check only (below)."""
    assert _read_leverage(value) == expected


@pytest.mark.parametrize("lev", [0.5, 126.0, 200.0, 1000.0])
def test_the_precheck_refuses_an_implausible_leverage_and_the_maximum_is_configurable(lev):
    r = _check(leverage=lev, stop_price=99.99)
    assert not r.viable and r.reason == risk.INPUTS_INVALID
    wide = _check(leverage=lev, stop_price=99.99, max_leverage=1000.0) if lev >= 1.0 else None
    if wide is not None:
        assert wide.reason != risk.INPUTS_INVALID  # accepted as plausible once the configured maximum allows it


def test_real_ccxt_parsing_of_leverage_brackets_feeds_the_calculation():
    import asyncio

    stub = _CcxtStub(brackets=BRACKETS)
    tiers = asyncio.run(exchange.fetch_maintenance_tiers(stub, "XRP/USDT:USDT"))
    assert stub.tier_params == {"symbol": "XRPUSDT"}  # asks for the one symbol, not the whole exchange
    assert [(t.tier, t.floor, t.cap, t.rate, t.cum) for t in tiers] == [
        (1, 0.0, 10000.0, 0.005, 0.0), (2, 10000.0, 50000.0, 0.01, 50.0), (3, 50000.0, 250000.0, 0.025, 800.0)]
    r = _check(tiers=tiers, entry_price=2.0, stop_price=1.96, quantity=15_000.0, leverage=20.0, effective_capital=1_000.0)
    assert r.tier == 2 and r.notional == 30_000.0
    assert r.effective_maint_rate == pytest.approx(0.01 - 50.0 / 30_000.0, abs=1e-6)


def test_a_failing_or_incomplete_bracket_read_is_none():
    import asyncio

    assert asyncio.run(exchange.fetch_maintenance_tiers(_CcxtStub(brackets=BRACKETS, raises=("tiers",)), "X")) is None
    no_cum = {"symbol": "XRPUSDT", "brackets": [{k: v for k, v in b.items() if k != "cum"} for b in BRACKETS["brackets"]]}
    assert asyncio.run(exchange.fetch_maintenance_tiers(_CcxtStub(brackets=no_cum), "X")) is None
    empty = {"symbol": "XRPUSDT", "brackets": []}
    assert asyncio.run(exchange.fetch_maintenance_tiers(_CcxtStub(brackets=empty), "X")) is None


def test_open_position_count_is_reliable_or_none():
    import asyncio

    flat = [{"symbol": "A", "contracts": 0.0, "info": {"positionAmt": "0"}}, {"symbol": "B", "contracts": None, "info": {"positionAmt": "0.0"}}]
    assert asyncio.run(exchange.fetch_open_position_count(_CcxtStub(positions=flat))) == 0
    two = flat + [{"symbol": "C", "contracts": 3.0}, {"symbol": "D", "contracts": None, "info": {"positionAmt": "-2"}}]
    assert asyncio.run(exchange.fetch_open_position_count(_CcxtStub(positions=two))) == 2
    for bad in (None, "x", [None], [{"symbol": "A"}], [{"symbol": "A", "contracts": "abc"}], [{"symbol": "A", "contracts": float("nan")}]):
        assert asyncio.run(exchange.fetch_open_position_count(_CcxtStub(positions=bad))) is None
    assert asyncio.run(exchange.fetch_open_position_count(_CcxtStub(positions=flat, raises=("positions",)))) is None


# ------------------------------------------------------------------ configuration


@pytest.mark.parametrize(
    "raw, mode, unrecognised",
    [
        (None, "observe", False), ("", "observe", False), ("observe", "observe", False), ("OBSERVE", "observe", False),
        ("off", "off", False), ("0", "off", False), ("false", "off", False), ("no", "off", False),
        ("enforce", "enforce", False), ("1", "enforce", False), ("true", "enforce", False), ("on", "enforce", False),
        ("enforced", "observe", True), ("yes please", "observe", True), ("2", "observe", True),
    ],
)
def test_guard_mode_parsing(raw, mode, unrecognised):
    assert parse_guard_mode(raw) == mode
    assert guard_mode_is_unrecognised(raw) is unrecognised


def test_shipped_defaults(monkeypatch):
    for var in ("PROTECTION_PRECHECK_MODE", "PROTECTION_FAIL_LIMIT_MODE", "PRECHECK_MIN_MAINT_RATE", "PRECHECK_STOP_SAFETY_FRACTION",
                "PRECHECK_CROSS_COLLATERAL_FACTOR", "PROTECTION_FAIL_LIMIT", "PROTECTION_FAIL_WINDOW_HOURS", "PROTECTION_ALERT_REPEAT_HOURS",
                "PRECHECK_MAX_LEVERAGE", "PROTECTION_TRANSIENT_LIMIT", "PROTECTION_TRANSIENT_WINDOW_HOURS",
                "PROTECTION_UNCLASSIFIED_LIMIT", "PROTECTION_UNCLASSIFIED_WINDOW_HOURS"):
        monkeypatch.delenv(var, raising=False)
    s = config_mod.load_settings()
    assert (s.protection_precheck_mode, s.protection_fail_limit_mode) == ("observe", "observe")
    assert (s.precheck_min_maint_rate, s.precheck_stop_safety_fraction, s.precheck_cross_collateral_factor) == (0.005, 0.75, 0.9)
    assert (s.protection_fail_limit, s.protection_fail_window_hours, s.protection_alert_repeat_hours) == (2, 24.0, 6.0)
    assert (s.protection_transient_limit, s.protection_transient_window_hours) == (3, 6.0)
    assert (s.protection_unclassified_limit, s.protection_unclassified_window_hours) == (3, 24.0)
    assert s.precheck_max_leverage == 125.0


def test_environment_overrides_and_clamping(monkeypatch):
    monkeypatch.setenv("PROTECTION_PRECHECK_MODE", "enforce")
    monkeypatch.setenv("PROTECTION_FAIL_LIMIT_MODE", "off")
    monkeypatch.setenv("PRECHECK_MIN_MAINT_RATE", "9")
    monkeypatch.setenv("PRECHECK_STOP_SAFETY_FRACTION", "0")
    monkeypatch.setenv("PROTECTION_FAIL_LIMIT", "-4")
    monkeypatch.setenv("PRECHECK_MAX_LEVERAGE", "5000")
    monkeypatch.setenv("PROTECTION_TRANSIENT_LIMIT", "-1")
    monkeypatch.setenv("PROTECTION_TRANSIENT_WINDOW_HOURS", "0")
    s = config_mod.load_settings()
    assert s.precheck_max_leverage == 1000.0 and s.protection_transient_limit == 0 and s.protection_transient_window_hours == 0.1
    assert (s.protection_precheck_mode, s.protection_fail_limit_mode) == ("enforce", "off")
    assert s.precheck_min_maint_rate == 0.2 and s.precheck_stop_safety_fraction == 0.1 and s.protection_fail_limit == 0


def test_the_fixtures_run_the_guards_enforced():
    s = make_settings()
    assert (s.protection_precheck_mode, s.protection_fail_limit_mode) == ("enforce", "enforce")


# ------------------------------------------------------------------ in the entry path
# helpers_live's synthetic candles put the stop ~52% from entry: fine at 1x, refused at 2x (allowed ~36%).


def _settings(**over):
    base = dict(max_notional_multiplier=100.0, trade_telemetry=True)
    base.update(over)
    return make_settings(**base)


def _alerts(notifier):
    return [t for t in notifier.texts if t.startswith("MBA Bot did not open a trade")]


def _decisions(reason=None):
    with session_scope() as session:
        rows = session.query(DecisionLog).all()
    return [d for d in rows if reason is None or d.reason_code == reason]


def _last_error(account_id):
    with session_scope() as session:
        return session.get(TradingAccount, account_id).last_error


def _fut(**kw):
    kw.setdefault("leverage_by_symbol", {"BTC/USDT": 2.0})
    return futures_client(**kw)


def _live(db_, monkeypatch, settings, client):
    install(monkeypatch, client)
    return new_account(settings, market_type="futures")


def test_enforce_refuses_before_any_order_with_a_decision_row_and_one_alert(db, monkeypatch):
    settings = _settings()
    client = _fut()
    account_id = _live(db, monkeypatch, settings, client)

    notifier = run_cycle(account_id, settings)

    assert client.market_orders == [] and all_trades(account_id) == []
    rows = _decisions(telemetry.R_STOP_UNVIABLE)
    assert len(rows) == 1 and rows[0].outcome == telemetry.REJECTED and rows[0].stage == telemetry.STAGE_ENTRY
    detail = json.loads(rows[0].detail_json)
    for key in ("leverage", "stop_distance_frac", "liquidation_distance_frac", "max_allowed_stop_frac", "margin_mode", "tier",
                "tier_rate", "tier_cum", "notional", "effective_maint_rate", "mode", "min_maint_rate", "safety_fraction"):
        assert key in detail, key
    assert detail["mode"] == "enforce" and 0 < detail["stop_distance_frac"] < 1  # a fraction, not percent
    assert "secret" not in rows[0].detail_json.lower() and "api_key" not in rows[0].detail_json.lower()
    assert len(_alerts(notifier)) == 1 and "leverage" in _alerts(notifier)[0].lower()
    assert "leverage" in _last_error(account_id).lower()
    notifier2 = run_cycle(account_id, settings, notifier)
    assert client.market_orders == [] and len(_alerts(notifier2)) == 1


def test_a_viable_entry_is_untouched_and_its_accepted_row_carries_the_check_numbers(db, monkeypatch):
    settings = _settings()
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0})
    account_id = _live(db, monkeypatch, settings, client)

    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert _decisions(telemetry.R_STOP_UNVIABLE) == []
    accepted = _decisions(telemetry.R_ENTRY_SUBMITTED)[0]
    pre = json.loads(accepted.detail_json)["precheck"]
    assert pre["margin_basis"] == "isolated" and pre["tier"] == 1
    # Units: the same fraction in the decision log and in trade_telemetry.
    with session_scope() as session:
        tt = session.query(TradeTelemetry).one()
        assert tt.stop_distance_pct == pytest.approx(pre["stop_distance_frac"], rel=1e-3)


def test_observe_records_the_would_be_refusal_and_blocks_nothing(db, monkeypatch):
    settings = _settings(protection_precheck_mode="observe")
    client = _fut()
    account_id = _live(db, monkeypatch, settings, client)

    notifier = run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open", "observe never blocks the entry"
    rows = _decisions(telemetry.R_STOP_UNVIABLE)
    assert len(rows) == 1 and rows[0].outcome == telemetry.OBSERVED
    detail = json.loads(rows[0].detail_json)
    assert detail["would_block"] is True and detail["mode"] == "observe"
    assert _alerts(notifier) == [], "observe sends no customer alert"
    assert _last_error(account_id) is None, "observe sets no dashboard error"


def test_observe_logs_to_the_journal_even_without_telemetry(db, monkeypatch, caplog):
    import logging

    settings = _settings(protection_precheck_mode="observe", trade_telemetry=False)
    client = _fut()
    account_id = _live(db, monkeypatch, settings, client)

    with caplog.at_level(logging.WARNING, logger="mba_bot.trader"):
        run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open" and _decisions() == []
    assert any("[observe] protection pre-check would refuse" in r.getMessage() for r in caplog.records)


def test_off_evaluates_and_reads_nothing(db, monkeypatch):
    settings = _settings(protection_precheck_mode="off", protection_fail_limit_mode="off")
    client = _fut()
    account_id = _live(db, monkeypatch, settings, client)

    run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open"
    assert not [c for c in client.calls if c[0] == "fetch_leverage_tiers"]
    assert _decisions(telemetry.R_STOP_UNVIABLE) == []


def test_spot_is_never_subject_to_the_check(db, monkeypatch):
    settings = _settings()
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="spot")

    run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open"
    assert not [c for c in client.calls if c[0] in ("fetch_leverage_tiers", "fetch_leverage")]


@pytest.mark.parametrize(
    "client_kw, reason",
    [
        (dict(maintenance_tiers=[]), telemetry.R_TIERS_UNAVAILABLE),                         # no brackets for the symbol
        (dict(maintenance_tiers=[{"bracket": 1, "initialLeverage": 5, "notionalCap": 10, "notionalFloor": 0,
                                  "maintMarginRatio": 0.004, "cum": 0.0}]), telemetry.R_TIERS_UNAVAILABLE),  # notional beyond the last cap
        (dict(maintenance_tiers=[{"bracket": 1, "notionalCap": 1e9, "notionalFloor": 0, "maintMarginRatio": 0.004}]), telemetry.R_TIERS_UNAVAILABLE),  # no cum
        (dict(margin_mode=None), telemetry.R_MARGIN_MODE_UNKNOWN),
    ],
)
def test_unreadable_exchange_inputs_refuse_before_any_order(db, monkeypatch, client_kw, reason):
    settings = _settings()
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0}, **client_kw)
    account_id = _live(db, monkeypatch, settings, client)

    notifier = run_cycle(account_id, settings)

    assert client.market_orders == [] and all_trades(account_id) == []
    assert len(_decisions(reason)) == 1 and len(_alerts(notifier)) == 1


def test_a_failing_bracket_read_refuses_before_any_order(db, monkeypatch):
    settings = _settings()
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0})
    client._tiers_raise = True
    account_id = _live(db, monkeypatch, settings, client)

    run_cycle(account_id, settings)

    assert client.market_orders == [] and len(_decisions(telemetry.R_TIERS_UNAVAILABLE)) == 1


def test_an_unexpected_error_inside_the_check_refuses_the_entry(db, monkeypatch):
    from mba_bot import trader

    def boom(**kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(trader, "assess_stop_viability", boom)
    settings = _settings()
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0})
    account_id = _live(db, monkeypatch, settings, client)

    run_cycle(account_id, settings)

    assert client.market_orders == [] and all_trades(account_id) == []
    assert len(_decisions(telemetry.R_PROTECTION_INPUTS_INVALID)) == 1


def test_cross_margin_with_other_open_positions_is_refused_before_any_order(db, monkeypatch):
    settings = _settings()
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0}, margin_mode="cross",
                  other_positions=[{"symbol": "ETH/USDT:USDT", "contracts": 0.5, "info": {"positionAmt": "0.5"}}])
    account_id = _live(db, monkeypatch, settings, client)

    notifier = run_cycle(account_id, settings)

    assert client.market_orders == [] and len(_decisions(telemetry.R_CROSS_UNVERIFIABLE)) == 1
    assert "cross margin" in _alerts(notifier)[0]


def test_cross_margin_on_a_clean_account_is_allowed_and_reads_the_account_positions(db, monkeypatch):
    settings = _settings()
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0}, margin_mode="cross")
    account_id = _live(db, monkeypatch, settings, client)

    run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open"
    assert ("fetch_positions", ()) in client.calls  # the account-wide read, with no symbol filter


def test_isolated_margin_never_reads_account_positions(db, monkeypatch):
    settings = _settings()
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0})
    account_id = _live(db, monkeypatch, settings, client)

    run_cycle(account_id, settings)

    assert ("fetch_positions", ()) not in client.calls


def _short_series():
    """The bullish fixture mirrored (price -> 200 - price): a bearish crossover on its last candle (entry 110, ATR
    23.5, stop 157 = ~43% above entry)."""
    up = [100, 95, 90, 85, 80, 75, 70, 65, 60, 58, 90]
    return [[i, 200 - c, 200 - c + 5.0, 200 - c - 5.0, 200 - c, 1.0] for i, c in enumerate(up)]


def test_the_short_fixture_really_is_a_bearish_crossover():
    sig = generate_signal(
        candles_from_ohlcv(_short_series()), ema_fast_period=STRATEGY_KWARGS["ema_fast_period"],
        ema_slow_period=STRATEGY_KWARGS["ema_slow_period"], atr_period=STRATEGY_KWARGS["atr_period"],
        atr_flat_threshold=STRATEGY_KWARGS["atr_flat_threshold"], atr_rolling_window=STRATEGY_KWARGS["atr_rolling_window"],
    )
    assert sig.signal == SignalType.BEARISH_CROSSOVER


def test_a_short_entry_goes_through_the_same_check(db, monkeypatch):
    settings = _settings()
    refused = _fut(ohlcv=_short_series(), leverage_by_symbol={"BTC/USDT": 125.0})
    account_id = _live(db, monkeypatch, settings, refused)
    run_cycle(account_id, settings)
    assert refused.market_orders == [] and len(_decisions(telemetry.R_STOP_UNVIABLE)) == 1
    assert json.loads(_decisions(telemetry.R_STOP_UNVIABLE)[0].detail_json)["side"] == "short"


def test_a_viable_short_entry_opens_and_is_protected(db, monkeypatch):
    settings = _settings()
    client = _fut(ohlcv=_short_series(), leverage_by_symbol={"BTC/USDT": 1.0})
    account_id = _live(db, monkeypatch, settings, client)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.side == "short" and trade.status == "open" and trade.protection_status == "protected"


# ------------------------------------------------------------------ failure classification


def _stop_failure_client(**kw):
    return _fut(leverage_by_symbol={"BTC/USDT": 1.0}, **kw)


def _close_reason(account_id):
    return the_trade(account_id).close_reason


@pytest.mark.parametrize(
    "exc, kind, reason, phrase",
    [
        (ccxt.RequestTimeout("t"), "transient", "protection_transient_emergency_exit", "temporary"),
        (ccxt.ExchangeNotAvailable("down"), "transient", "protection_transient_emergency_exit", "temporary"),
        (ccxt.InvalidOrder("-2021 would immediately trigger"), "structural", "protection_failed_emergency_exit", "cannot hold a protective stop"),
        (ccxt.BadRequest("bad"), "structural", "protection_failed_emergency_exit", "cannot hold a protective stop"),
        (RuntimeError("something odd"), "other", "protection_unclassified_emergency_exit", "could not be confirmed"),
    ],
)
def test_failures_are_classified_and_the_exit_message_names_the_cause(db, monkeypatch, exc, kind, reason, phrase):
    settings = _settings(protection_fail_limit_mode="off")
    client = _stop_failure_client()
    client.fail("create_order", role="stop", exc=exc, times=99)
    account_id = _live(db, monkeypatch, settings, client)

    notifier = run_cycle(account_id, settings)

    assert _close_reason(account_id) == reason
    assert any(phrase in t for t in notifier.texts), notifier.texts
    assert lifecycle.classify_protection_failure(exc) == kind


def test_a_stop_beyond_liquidation_is_structural_and_names_the_check(db, monkeypatch):
    settings = _settings(protection_precheck_mode="off", protection_fail_limit_mode="off")
    client = _stop_failure_client(liquidation_price=60.0)
    account_id = _live(db, monkeypatch, settings, client)

    notifier = run_cycle(account_id, settings)

    assert _close_reason(account_id) == "protection_failed_emergency_exit"
    assert any("liquidation price" in t for t in notifier.texts)


# ------------------------------------------------------------------ repeated failures


def _failed_exit_rows(account_id, count, *, ago_hours=1.0, pair="BTC/USDT", reason="protection_failed_emergency_exit", paper=False):
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for i in range(count):
            t = now - timedelta(hours=ago_hours, minutes=i)
            session.add(Trade(account_id=account_id, side="long", is_paper=paper, entry_price=10.0, quantity=1.0, stop_loss_price=9.0,
                              status="closed", opened_at=t, closed_at=t, pair=pair, close_reason=reason))


def _limit_client():
    return _fut(leverage_by_symbol={"BTC/USDT": 1.0})


def test_enforce_blocks_after_the_limit_with_a_decision_row_and_one_alert(db, monkeypatch):
    settings = _settings(protection_precheck_mode="off", protection_fail_limit=2)
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 2)

    notifier = run_cycle(account_id, settings)

    assert client.market_orders == []
    rows = _decisions(telemetry.R_PROTECTION_FAIL_LIMIT)
    assert len(rows) == 1 and rows[0].outcome == telemetry.REJECTED and "failures" in rows[0].detail_json
    assert len(_alerts(notifier)) == 1 and "paused" in _alerts(notifier)[0].lower()
    assert len(all_trades(account_id)) == 2
    notifier2 = run_cycle(account_id, settings, notifier)
    assert client.market_orders == [] and len(_alerts(notifier2)) == 1


def test_observe_records_the_limit_and_blocks_nothing(db, monkeypatch):
    settings = _settings(protection_precheck_mode="off", protection_fail_limit=2, protection_fail_limit_mode="observe")
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 3)

    notifier = run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open"
    rows = _decisions(telemetry.R_PROTECTION_FAIL_LIMIT)
    assert len(rows) == 1 and rows[0].outcome == telemetry.OBSERVED
    assert json.loads(rows[0].detail_json)["would_block"] is True
    assert _alerts(notifier) == [] and _last_error(account_id) is None


def test_off_and_zero_limit_never_block(db, monkeypatch):
    for n, kw in enumerate((dict(protection_fail_limit_mode="off"), dict(protection_fail_limit=0))):
        settings = _settings(protection_precheck_mode="off", **kw)
        client = _limit_client()
        install(monkeypatch, client)
        account_id = new_account(settings, market_type="futures", email=f"limit{n}@example.com")
        _failed_exit_rows(account_id, 10)
        run_cycle(account_id, settings)
        assert the_trade(account_id).status == "open", kw


def test_only_structural_failures_inside_the_window_on_this_pair_count(db, monkeypatch):
    # The transient and unclassified limits have their own tests below; here they are off so this one isolates the
    # structural count (transient and unclassified rows are seeded on purpose and must not feed it).
    settings = _settings(protection_precheck_mode="off", protection_fail_limit=2, protection_fail_window_hours=24.0,
                         protection_transient_limit=0, protection_unclassified_limit=0)
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 5, ago_hours=30.0)                                        # too old
    _failed_exit_rows(account_id, 5, pair="XRP/USDT")                                       # another pair
    _failed_exit_rows(account_id, 5, reason="crossover_exit")                               # not a protection failure
    _failed_exit_rows(account_id, 5, reason="protection_transient_emergency_exit")          # transient exchange trouble
    _failed_exit_rows(account_id, 5, reason="protection_unclassified_emergency_exit")       # unclassified
    _failed_exit_rows(account_id, 5, paper=True)                                            # paper
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open"


def test_one_failure_below_the_limit_does_not_block(db, monkeypatch):
    settings = _settings(protection_precheck_mode="off", protection_fail_limit=2)
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 1)
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open"


def test_the_failure_ages_out_of_the_window(db, monkeypatch):
    settings = _settings(protection_precheck_mode="off", protection_fail_limit=2, protection_fail_window_hours=24.0)
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 2, ago_hours=23.9)
    with session_scope() as session:
        assert protection_guard.recent_protection_failures(session, account_id, "BTC/USDT", now=datetime.now(timezone.utc), window_hours=24.0) == 2
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        assert protection_guard.recent_protection_failures(session, account_id, "BTC/USDT", now=later, window_hours=24.0) == 0


def test_the_incident_loop_is_cut_off_after_the_limit_end_to_end(db, monkeypatch):
    """Pre-check off (the old behaviour); the stop is beyond liquidation every time. Two entries open and are
    emergency-closed, then the limit stops the loop instead of a 23-trade run."""
    settings = _settings(protection_precheck_mode="off", protection_fail_limit=2)
    client = _fut(liquidation_price=60.0, leverage_by_symbol={"BTC/USDT": 1.0})
    account_id = _live(db, monkeypatch, settings, client)

    notifier = None
    for _ in range(6):
        notifier = run_cycle(account_id, settings, notifier)

    structural = [t for t in all_trades(account_id) if t.close_reason == "protection_failed_emergency_exit"]
    assert len(structural) == 2 and len(client.market_orders) == 4
    assert len(_alerts(notifier)) == 1


def test_transient_failures_do_not_count_toward_the_structural_limit_end_to_end(db, monkeypatch):
    """Structural limit 1: if transient exits were counted by it, the second round would already be blocked."""
    settings = _settings(protection_precheck_mode="off", protection_fail_limit=1, protection_transient_limit=0)
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0})
    client.fail("create_order", role="stop", exc=ccxt.RequestTimeout("slow"), times=999)
    account_id = _live(db, monkeypatch, settings, client)

    notifier = None
    for _ in range(4):
        notifier = run_cycle(account_id, settings, notifier)

    closed = [t for t in all_trades(account_id) if t.close_reason == "protection_transient_emergency_exit"]
    assert len(closed) == 4, "neither limit engaged: the structural one ignores transient exits, the transient one is off"
    assert _alerts(notifier) == []


def test_a_transient_loop_is_cut_off_after_its_own_limit_end_to_end(db, monkeypatch):
    settings = _settings(protection_precheck_mode="off", protection_transient_limit=3)
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0})
    client.fail("create_order", role="stop", exc=ccxt.RequestTimeout("slow"), times=999)
    account_id = _live(db, monkeypatch, settings, client)

    notifier = None
    for _ in range(7):
        notifier = run_cycle(account_id, settings, notifier)

    closed = [t for t in all_trades(account_id) if t.close_reason == "protection_transient_emergency_exit"]
    assert len(closed) == 3 and len(client.market_orders) == 6  # three entries and their three emergency exits
    alerts = _alerts(notifier)
    assert len(alerts) == 1 and "not caused by your settings" in alerts[0]
    assert len(_decisions(telemetry.R_TRANSIENT_FAIL_LIMIT)) >= 1
    assert _decisions(telemetry.R_PROTECTION_FAIL_LIMIT) == []


def test_a_failing_lookup_never_blocks_trading():
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

    assert protection_guard.limit_reached(Boom(), "a", "BTC/USDT", now=datetime.now(timezone.utc), limit=2, window_hours=24.0) is None


def test_alert_repeat_window():
    protection_guard.reset_for_tests()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert protection_guard.should_alert("a", "BTC/USDT", "r", now=t0, repeat_hours=6)
    assert not protection_guard.should_alert("a", "BTC/USDT", "r", now=t0 + timedelta(hours=5), repeat_hours=6)
    assert protection_guard.should_alert("a", "BTC/USDT", "r", now=t0 + timedelta(hours=6, minutes=1), repeat_hours=6)
    assert protection_guard.should_alert("a", "XRP/USDT", "r", now=t0, repeat_hours=6)
    protection_guard.reset_for_tests()


def test_the_journal_names_the_refusing_check_and_the_category(db, monkeypatch, caplog):
    import logging

    settings = _settings(protection_precheck_mode="off", protection_fail_limit_mode="off")
    client = _stop_failure_client(liquidation_price=60.0)
    account_id = _live(db, monkeypatch, settings, client)

    with caplog.at_level(logging.WARNING, logger="mba_bot.lifecycle"):
        run_cycle(account_id, settings)

    assert any("failed (ProtectionUnavailable: stop is at or beyond the liquidation price) [structural]" in r.getMessage()
               for r in caplog.records)


def test_reason_codes_match_between_risk_and_telemetry():
    assert (risk.STOP_UNVIABLE, risk.INPUTS_INVALID, risk.TIERS_UNAVAILABLE, risk.MARGIN_MODE_UNKNOWN, risk.CROSS_UNVERIFIABLE) == (
        telemetry.R_STOP_UNVIABLE, telemetry.R_PROTECTION_INPUTS_INVALID, telemetry.R_TIERS_UNAVAILABLE,
        telemetry.R_MARGIN_MODE_UNKNOWN, telemetry.R_CROSS_UNVERIFIABLE)
    assert all(len(c) <= 48 for c in (risk.STOP_UNVIABLE, risk.TIERS_UNAVAILABLE, risk.CROSS_UNVERIFIABLE, risk.MARGIN_MODE_UNKNOWN))


# ------------------------------------------------------------------ the transient and unclassified limits

TRANSIENT = "protection_transient_emergency_exit"
UNCLASSIFIED = "protection_unclassified_emergency_exit"
STRUCTURAL = "protection_failed_emergency_exit"


def _quiet_settings(**over):
    """Pre-check off and every limit at its default; tests override what they exercise."""
    return _settings(protection_precheck_mode="off", **over)


def test_transient_exits_below_the_limit_do_not_block_and_at_the_limit_do(db, monkeypatch):
    settings = _quiet_settings()
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 2, reason=TRANSIENT)
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open", "2 transient exits in 6h is below the limit of 3"


def test_transient_limit_blocks_with_a_cause_specific_alert(db, monkeypatch):
    settings = _quiet_settings()
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 3, reason=TRANSIENT, ago_hours=1.0)

    notifier = run_cycle(account_id, settings)

    assert client.market_orders == []
    rows = _decisions(telemetry.R_TRANSIENT_FAIL_LIMIT)
    detail = json.loads(rows[0].detail_json)
    assert len(rows) == 1 and rows[0].outcome == telemetry.REJECTED
    assert (detail["kind"], detail["scope"], detail["failures"], detail["limit"], detail["window_hours"]) == ("transient", "account", 3, 3, 6.0)
    alert = _alerts(notifier)[0]
    assert "could not be reached reliably" in alert and "resume automatically" in alert and "leverage" not in alert.lower()


def test_transient_scope_is_the_whole_account_not_one_pair(db, monkeypatch):
    settings = _quiet_settings()
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 3, reason=TRANSIENT, pair="XRP/USDT")  # other pair, same account
    run_cycle(account_id, settings)
    assert client.market_orders == [], "an exchange problem is not specific to the pair that happened to be traded"


def test_transient_rows_of_another_account_or_paper_or_outside_six_hours_do_not_count(db, monkeypatch):
    settings = _quiet_settings()
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    other_id = new_account(settings, market_type="futures", email="other@example.com")
    _failed_exit_rows(other_id, 5, reason=TRANSIENT)                    # another account
    _failed_exit_rows(account_id, 5, reason=TRANSIENT, paper=True)      # paper
    _failed_exit_rows(account_id, 5, reason=TRANSIENT, ago_hours=7.0)   # older than the 6h window
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open"


def test_the_transient_lockout_ends_by_itself_when_the_oldest_exit_leaves_the_window(db, monkeypatch):
    settings = _quiet_settings()
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 3, reason=TRANSIENT, ago_hours=5.9)
    with session_scope() as session:
        now = datetime.now(timezone.utc)
        assert protection_guard.count_failures(session, account_id, reason=TRANSIENT, now=now, window_hours=6.0) == 3
        assert protection_guard.count_failures(session, account_id, reason=TRANSIENT, now=now + timedelta(hours=1), window_hours=6.0) == 0


def test_unclassified_limit_is_per_account_and_pair(db, monkeypatch):
    settings = _quiet_settings()
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 3, reason=UNCLASSIFIED, pair="XRP/USDT")  # another pair: must NOT lock out BTC
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open"

    protection_guard.reset_for_tests()
    client2 = _limit_client()
    install(monkeypatch, client2)
    account2 = new_account(settings, market_type="futures", email="second@example.com")
    _failed_exit_rows(account2, 3, reason=UNCLASSIFIED, pair="BTC/USDT")
    notifier = run_cycle(account2, settings)
    assert client2.market_orders == []
    rows = _decisions(telemetry.R_UNCLASSIFIED_FAIL_LIMIT)
    assert len(rows) == 1 and json.loads(rows[0].detail_json)["scope"] == "account_pair"
    assert "could not identify" in _alerts(notifier)[0]


def test_two_unclassified_exits_do_not_block(db, monkeypatch):
    settings = _quiet_settings()
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 2, reason=UNCLASSIFIED)
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open"


def test_the_categories_do_not_feed_each_other(db, monkeypatch):
    """1 structural + 2 transient + 2 unclassified: each is below its own limit, so nothing blocks."""
    settings = _quiet_settings()
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 1, reason=STRUCTURAL)
    _failed_exit_rows(account_id, 2, reason=TRANSIENT)
    _failed_exit_rows(account_id, 2, reason=UNCLASSIFIED)
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open"


def test_when_several_limits_are_reached_the_structural_one_is_reported_first(db, monkeypatch):
    settings = _quiet_settings()
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 2, reason=STRUCTURAL)
    _failed_exit_rows(account_id, 3, reason=TRANSIENT)
    notifier = run_cycle(account_id, settings)
    assert client.market_orders == [] and "leverage" in _alerts(notifier)[0].lower()
    assert len(_decisions(telemetry.R_PROTECTION_FAIL_LIMIT)) == 1


def test_each_limit_can_be_switched_off_on_its_own(db, monkeypatch):
    for n, kw in enumerate((dict(protection_transient_limit=0), dict(protection_unclassified_limit=0))):
        settings = _quiet_settings(**kw)
        client = _limit_client()
        install(monkeypatch, client)
        account_id = new_account(settings, market_type="futures", email=f"sw{n}@example.com")
        _failed_exit_rows(account_id, 9, reason=TRANSIENT if "protection_transient_limit" in kw else UNCLASSIFIED)
        run_cycle(account_id, settings)
        assert the_trade(account_id).status == "open", kw


def test_observe_records_every_limit_reached_and_blocks_nothing(db, monkeypatch):
    settings = _quiet_settings(protection_fail_limit_mode="observe")
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 2, reason=STRUCTURAL)
    _failed_exit_rows(account_id, 3, reason=TRANSIENT)
    _failed_exit_rows(account_id, 3, reason=UNCLASSIFIED)

    notifier = run_cycle(account_id, settings)

    assert the_trade(account_id).status == "open" and _alerts(notifier) == [] and _last_error(account_id) is None
    codes = {d.reason_code for d in _decisions() if d.outcome == telemetry.OBSERVED}
    assert codes == {telemetry.R_PROTECTION_FAIL_LIMIT, telemetry.R_TRANSIENT_FAIL_LIMIT, telemetry.R_UNCLASSIFIED_FAIL_LIMIT}


def test_a_broken_lookup_of_one_category_neither_blocks_nor_hides_the_others(db, monkeypatch):
    settings = _quiet_settings()
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 3, reason=UNCLASSIFIED)
    real = protection_guard.count_failures

    def flaky(session, account_id, *, reason, **kw):
        if reason == TRANSIENT:
            raise RuntimeError("db hiccup")
        return real(session, account_id, reason=reason, **kw)

    monkeypatch.setattr(protection_guard, "count_failures", flaky)
    run_cycle(account_id, settings)
    assert client.market_orders == [], "the unclassified limit still applied although the transient lookup failed"


# ------------------------------------------------------------------ classification is reset per attempt


def test_the_failure_classification_is_reset_for_every_attempt_and_call(db, monkeypatch):
    import asyncio

    settings = make_settings(protection_max_attempts=3)
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0})
    account_id = _live(db, monkeypatch, settings, client)
    outcomes = []

    async def scripted(ctx, trade):
        step = outcomes.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    monkeypatch.setattr(lifecycle, "_place_and_verify", scripted)
    with session_scope() as session:
        ctx = live_context(session, account_id, client, settings)
        trade = Trade(account_id=account_id, side="long", is_paper=False, entry_price=10.0, quantity=1.0, stop_loss_price=9.0,
                      status="protecting", opened_at=datetime.now(timezone.utc), pair="BTC/USDT")
        session.add(trade)
        session.flush()

        ctx.protection_failure_kind = "structural"   # stale value from some earlier call on this context
        outcomes[:] = [ccxt.RequestTimeout("t"), ccxt.RequestTimeout("t"), ccxt.RequestTimeout("t")]
        assert asyncio.run(lifecycle.establish_protection(ctx, trade)) == "failed"
        assert ctx.protection_failure_kind == "transient", "the stale 'structural' must not survive"

        outcomes[:] = [ccxt.RequestTimeout("t"), "protected"]   # a failed attempt followed by a good one
        assert asyncio.run(lifecycle.establish_protection(ctx, trade)) == "protected"
        assert ctx.protection_failure_kind is None and ctx.protection_failure_detail is None

        outcomes[:] = [ccxt.RequestTimeout("t"), ProtectionUnavailable("stop is at or beyond the liquidation price")]
        assert asyncio.run(lifecycle.establish_protection(ctx, trade)) == "failed"
        assert ctx.protection_failure_kind == "structural"
        assert ctx.protection_failure_detail == "stop is at or beyond the liquidation price"

        outcomes[:] = [ProtectionUnavailable("x"), ]  # structural stops retrying at once
        assert asyncio.run(lifecycle.establish_protection(ctx, trade)) == "failed" and ctx.protection_failure_kind == "structural"
        session.rollback()



# ------------------------------------------------------------------ the leverage range does not apply with the guards off


def _lev_client(lev):
    return _fut(leverage_by_symbol={"BTC/USDT": lev})


def test_with_the_precheck_off_an_unusual_leverage_reads_and_trades_as_before(db, monkeypatch):
    settings = _settings(protection_precheck_mode="off", protection_fail_limit_mode="off")
    client = _lev_client(200.0)
    account_id = _live(db, monkeypatch, settings, client)
    run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open", "no guard runs, so the leverage value is not second-guessed"


def test_with_the_precheck_enforced_an_implausible_leverage_is_refused_before_any_order(db, monkeypatch):
    settings = _settings(protection_precheck_mode="enforce")
    client = _lev_client(200.0)
    account_id = _live(db, monkeypatch, settings, client)
    notifier = run_cycle(account_id, settings)
    assert client.market_orders == [] and len(_decisions(telemetry.R_PROTECTION_INPUTS_INVALID)) == 1
    assert len(_alerts(notifier)) == 1


def test_with_the_precheck_observing_an_implausible_leverage_is_recorded_and_not_blocked(db, monkeypatch):
    settings = _settings(protection_precheck_mode="observe")
    client = _lev_client(200.0)
    account_id = _live(db, monkeypatch, settings, client)
    notifier = run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open" and _alerts(notifier) == []
    assert [d.outcome for d in _decisions(telemetry.R_PROTECTION_INPUTS_INVALID)] == [telemetry.OBSERVED]


def test_the_configured_maximum_leverage_is_honoured(db, monkeypatch):
    settings = _settings(protection_precheck_mode="enforce", precheck_max_leverage=250.0)
    client = _lev_client(200.0)
    account_id = _live(db, monkeypatch, settings, client)
    run_cycle(account_id, settings)
    assert _decisions(telemetry.R_PROTECTION_INPUTS_INVALID) == []  # accepted as plausible; the stop is then judged on its merits


# ------------------------------------------------------------------ observe-mode logging


def test_an_observed_refusal_is_journaled_once_per_window_not_every_round(db, monkeypatch, caplog):
    import logging

    settings = _settings(protection_precheck_mode="observe", trade_telemetry=False)
    client = _fut()   # 2x leverage on the 52% synthetic stop: would be refused
    account_id = _live(db, monkeypatch, settings, client)

    with caplog.at_level(logging.INFO, logger="mba_bot.trader"):
        notifier = None
        for _ in range(4):
            notifier = run_cycle(account_id, settings, notifier)

    lines = [r for r in caplog.records if "[observe] protection pre-check would refuse" in r.getMessage()]
    assert len(lines) == 1, "a persistent would-be refusal must not repeat every round"


def test_an_observed_pass_is_journaled_even_without_telemetry_and_only_once_per_entry(db, monkeypatch, caplog):
    import logging

    settings = _settings(protection_precheck_mode="observe", trade_telemetry=False)
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0})
    account_id = _live(db, monkeypatch, settings, client)

    with caplog.at_level(logging.INFO, logger="mba_bot.trader"):
        run_cycle(account_id, settings)

    passes = [r.getMessage() for r in caplog.records if "[observe] protection pre-check passes" in r.getMessage()]
    assert len(passes) == 1 and "at 1x" in passes[0] and "bracket 1" in passes[0] and "isolated margin" in passes[0]
    assert _decisions() == [], "no telemetry, so the journal line is the only record"


def test_enforce_mode_writes_no_pass_line(db, monkeypatch, caplog):
    import logging

    settings = _settings(protection_precheck_mode="enforce", trade_telemetry=False)
    client = _fut(leverage_by_symbol={"BTC/USDT": 1.0})
    account_id = _live(db, monkeypatch, settings, client)
    with caplog.at_level(logging.INFO, logger="mba_bot.trader"):
        run_cycle(account_id, settings)
    assert not [r for r in caplog.records if "[observe]" in r.getMessage()]


def test_observed_limit_lines_are_journaled_once_per_window(caplog):
    """Calls the trader helper the way each round does: only the first line in a window is written."""
    import logging
    from types import SimpleNamespace

    from mba_bot import trader

    protection_guard.reset_for_tests()
    account = SimpleNamespace(id="acc", pair="BTC/USDT")
    settings = make_settings(protection_alert_repeat_hours=6.0)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with caplog.at_level(logging.WARNING, logger="mba_bot.trader"):
        for minutes in (0, 5, 10, 300, 359):
            trader._observe_line(account, settings, "some_reason", t0 + timedelta(minutes=minutes), "line %s", minutes)
        trader._observe_line(account, settings, "some_reason", t0 + timedelta(minutes=361), "line %s", 361)
        trader._observe_line(account, settings, "another_reason", t0, "line other")
    assert [r.getMessage() for r in caplog.records] == ["line 0", "line 361", "line other"]
    protection_guard.reset_for_tests()


def test_an_observed_limit_is_journaled_on_the_entry_that_follows(db, monkeypatch, caplog):
    import logging

    settings = _quiet_settings(protection_fail_limit_mode="observe", trade_telemetry=False)
    client = _limit_client()
    account_id = _live(db, monkeypatch, settings, client)
    _failed_exit_rows(account_id, 3, reason=UNCLASSIFIED)
    with caplog.at_level(logging.INFO, logger="mba_bot.trader"):
        run_cycle(account_id, settings)
    assert the_trade(account_id).status == "open"
    assert len([r for r in caplog.records if "[observe] unclassified protection-failure limit reached" in r.getMessage()]) == 1


# ------------------------------------------------------------------ frontend labels


def test_every_emergency_close_reason_has_an_explicit_customer_label():
    """frontend/src/constants.js closeReasonLabel falls back to "Signal exit", which would mislabel a safety exit."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "constants.js").read_text()
    body = text[text.index("export function closeReasonLabel"):]
    cases = set(re.findall(r'case "([a-z_]+)":', body[: body.index("default:")]))
    # Every reason lifecycle.py can pass to emergency_close, found in the source so a new one cannot be forgotten.
    source = (Path(lifecycle.__file__)).read_text()
    literal = set(re.findall(r'emergency_close\(\s*ctx, trade, reason="([a-z_]+)"', source))
    assert "quantity_unverifiable" in literal, "the source scan found nothing; the pattern needs updating"
    reasons = {lifecycle.EMERGENCY_REASON_STRUCTURAL, lifecycle.EMERGENCY_REASON_TRANSIENT, lifecycle.EMERGENCY_REASON_OTHER} | literal
    for reason in reasons:
        assert reason in cases, f"{reason} would be shown to the customer as a signal exit"
