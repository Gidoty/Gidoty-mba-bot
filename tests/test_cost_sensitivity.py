"""Cost sensitivity (src/mba_bot/cost_sensitivity.py). Research only.

PROVES: the baseline re-pricing reproduces each trade's recorded net R exactly; each scenario's arithmetic (BNB, tiers, maker entry, exits always taker); the miss-rate readings (random keeps the average, adverse drops the best
gross trades and so lowers it); the zero-cost row equals gross plus funding; the ordering of scenarios; and the plan hash.
DOES NOT PROVE anything about profit or about the real fee schedule (UNVERIFIED)."""
from __future__ import annotations

import pytest

from mba_bot import cost_sensitivity as CX
from mba_bot import cost_split as CS
from mba_bot import profit_backtest as P

HOUR = 3_600_000
BASE = CX.SCENARIOS[0][1]


def mk(side, raw_entry, raw_exit, dist, funding_r=0.0):
    legs = [P.Leg(1.0, raw_exit, 0, "x")]
    entry, gross, net, extra = P._cost(side, raw_entry, dist, legs, 0.0)
    return P.SimTrade("BTCUSDT", side, 0, HOUR, raw_entry, entry, dist, raw_entry - dist if side == "long" else raw_entry + dist, raw_entry, legs, "x", gross, net + funding_r, funding_r, extra, False, None, HOUR)


def test_the_baseline_reproduces_every_recorded_net_r_for_longs_and_shorts():
    for t in (mk("long", 100.0, 103.0, 2.0), mk("long", 100.0, 98.0, 2.0, -0.05), mk("short", 100.0, 97.0, 2.0, 0.02), mk("short", 100.0, 104.0, 2.0)):
        assert CX.recost(t, BASE) == pytest.approx(t.net_r, abs=1e-12)


def test_bnb_and_the_tiers_lower_the_fee_in_the_stated_amounts():
    t = mk("long", 100.0, 100.0, 2.0)
    base = CX.recost(t, BASE)
    bnb = CX.recost(t, {"tier": "regular", "bnb": True, "maker_entry": False})
    fee_saved = 0.10 * 5.0 / 1e4 * (100.0 * 1.00055 + 100.0 * 0.99945) / 2.0
    assert bnb - base == pytest.approx(fee_saved)
    assert CX.fee_bps({"tier": "vip3", "bnb": True, "maker_entry": False}) == pytest.approx((3.2 * 0.9, 1.2 * 0.9))
    levels = [CX.recost(t, {"tier": k, "bnb": False, "maker_entry": False}) for k in ("regular", "vip1", "vip2", "vip3")]
    assert levels == sorted(levels)  # a lower fee tier is never worse


def test_a_maker_entry_has_no_entry_slippage_the_maker_fee_and_the_exit_stays_a_market_order():
    t = mk("long", 100.0, 103.0, 2.0)
    cfg = {"tier": "regular", "bnb": False, "maker_entry": True}
    ex = 103.0 * (1 - 5.5e-4)
    want = ((ex - 100.0) - 2.0e-4 * 100.0 - 5.0e-4 * ex) / 2.0
    assert CX.recost(t, cfg) == pytest.approx(want)
    assert CX.recost(t, cfg) > CX.recost(t, BASE)
    s = mk("short", 100.0, 97.0, 2.0)
    exs = 97.0 * (1 + 5.5e-4)
    assert CX.recost(s, cfg) == pytest.approx(((100.0 - exs) - 2.0e-4 * 100.0 - 5.0e-4 * exs) / 2.0)


def test_random_misses_keep_the_average_and_adverse_misses_drop_the_best_trades():
    trades = [mk("long", 100.0, 100.0 + k, 2.0) for k in range(-4, 6)]  # ten trades, gross from -2R to +2.5R
    cfg = {"tier": "regular", "bnb": False, "maker_entry": True}
    all_ = CX.evaluate(trades, cfg, 0.0, "random")
    rnd = CX.evaluate(trades, cfg, 0.4, "random")
    adv = CX.evaluate(trades, cfg, 0.4, "adverse")
    assert rnd["mean"] == pytest.approx(all_["mean"]) and rnd["trades"] == 6 and all_["trades"] == 10
    assert adv["trades"] == 6 and adv["mean"] < all_["mean"]
    kept_gross = sorted(CX.gross(t) for t in trades)[:6]
    assert adv["mean"] == pytest.approx(sum(CX.recost(t, cfg) for t in trades if CX.gross(t) in kept_gross) / 6)
    taker = {"tier": "regular", "bnb": False, "maker_entry": False}
    assert CX.evaluate(trades, taker, 0.4, "adverse")["trades"] == 10  # the miss rate only applies to maker entries
    assert CX.evaluate([], cfg, 0.2, "random") == {"trades": 0}


def test_zero_cost_is_gross_plus_funding_and_every_scenario_beats_the_baseline_or_equals_it():
    ts = [mk("long", 100.0, 101.0 + 0.3 * k, 2.0, funding_r=-0.01) for k in range(8)]
    zero = sum(CX.gross(t) + t.funding_r for t in ts) / len(ts)
    for name, cfg in CX.SCENARIOS:
        r = CX.evaluate(ts, cfg, 0.0, "random")
        assert r["mean"] >= CX.evaluate(ts, BASE, 0.0, "random")["mean"] - 1e-12 and r["mean"] <= zero + 1e-12, name


def test_the_plan_hash_is_stable_and_changes_with_a_fee_or_a_source(monkeypatch):
    h = CX.plan_hash()
    assert h == CX.plan_hash() and len(h) == 16
    monkeypatch.setitem(CX.FEES, "vip3", (3.0, 1.2))
    assert CX.plan_hash() != h
    monkeypatch.undo()
    assert set(CX.source_pins()) >= {"cost_sensitivity.py", "cost_split.py"}
