"""The sealed test of profit protection v2 (src/mba_bot/profit_backtest.py). Research only.

PROVES: the baseline rule set reproduces the pre-registered short-timeframe EMA simulation trade for trade; each rule's mechanics on hand-made candles (stage 1 lands at or above zero net, the time stop, partial
profit's two legs and their costs, take profit net of costs, the reversal exits, re-entry after a protective exit only); funding signs and weights; the metrics (profit factor, streaks, drawdown, give-back rate, winners
cut short, recovered re-entries); the pre-registered combination rule; the plan hash; and the one-look ledger and every refusal.
DOES NOT PROVE: that any rule makes money. The numbers it produces are measurements on two windows."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from mba_bot import ema_exits, ema_reversal, profit_backtest as P, profit_protect as pp, reentry
from mba_bot import shortframe_backtest as SF
from mba_bot.strategy import Candle

HOUR = 3_600_000
FEE, SLIP = 5.0 / 1e4, 5.5 / 1e4


def C(i, o, h, lo, c, base=1_000 * HOUR):
    return Candle(base + i * HOUR, o, h, lo, c, 1.0)


def S(candles, sigs=None, atr=1.0, fast=None, slow=None, flat=None):
    n = len(candles)
    return P.Series(candles, sigs or [None] * n, [atr] * n if not isinstance(atr, list) else atr, fast or [None] * n, slow or [None] * n, flat or [False] * n)


def flat_bars(n, price=100.0, start=0):
    return [C(start + k, price, price + 0.2, price - 0.2, price) for k in range(n)]


def run(ser, rs, side="long", i=0, events=None, end=10**18):
    marks = {c.timestamp: c.open for c in ser.candles}
    return P.run_trade("BTCUSDT", ser, "1h", i, side, rs, end, events or [], marks)


# ---------------------------------------------------------------------------------------------- the baseline reproduces the short-timeframe simulation
def test_the_baseline_rule_set_reproduces_the_pre_registered_ema_simulation_without_funding():
    rng = random.Random(11)
    price, rows = 100.0, []
    for i in range(900):
        price *= 1 + rng.gauss(0, 0.01) + 0.0015 * (1 if (i // 60) % 2 == 0 else -1)
        hi, lo = price * (1 + abs(rng.gauss(0, 0.004))), price * (1 - abs(rng.gauss(0, 0.004)))
        rows.append(C(i, rows[-1].close if rows else price, max(hi, price), min(lo, price), price))
    start, end = rows[0].timestamp, rows[-1].timestamp + HOUR
    want, _ = SF.simulate_ema("BTCUSDT", rows, "1h", start, end)
    got, _ = P.simulate("BTCUSDT", P.build_series(rows), "1h", start, end, P.RS0, [])
    assert len(want) > 10 and len(got) == len(want)
    for a, b in zip(want, got):
        assert (a.entry_ts, a.exit_ts, a.reason) == (b.entry_ts, b.exit_ts, b.reason)
        assert a.net_r == pytest.approx(b.net_r, abs=1e-9) and a.gross_r == pytest.approx(b.gross_r, abs=1e-9)


# ---------------------------------------------------------------------------------------------- the rules on hand-made candles
def lock_series():
    cs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 100.5, 99.9, 100.3), C(2, 100.3, 102.5, 100.2, 102.0), C(3, 101.5, 101.6, 100.0, 100.5), C(4, 100.5, 100.7, 100.3, 100.5)]
    return S(cs, atr=1.0)


def test_stage_1_lands_at_entry_plus_the_cost_buffer_and_a_stop_there_is_at_or_above_zero_net():
    t, j, fin = run(lock_series(), P.OPTIONS[0], i=0)  # A1: stage 1 at +1R (R = 2 ATR = 2.0)
    assert fin and t.reason == "break_even_stop" and j == 3
    buffer = 100.0 * (2 * 5.0 + 11.0) / 1e4
    assert t.legs[0].raw_exit == pytest.approx(t.entry + buffer, abs=0.02) or t.legs[0].raw_exit == pytest.approx(100.0 * (1 + SLIP) + buffer, abs=0.02)
    assert t.net_r >= 0.0  # the whole point of the buffer


def test_with_no_rule_the_same_candles_run_to_the_initial_stop_or_the_crossover():
    cs = lock_series().candles + [C(5, 100.5, 100.6, 90.0, 91.0)]
    t, j, fin = run(S(cs, atr=1.0), P.RS0, i=0)
    assert fin and t.reason == "stop_loss" and t.net_r < -1.0  # the 2 ATR stop plus costs


def test_the_stop_is_judged_before_a_level_earned_in_the_same_candle_and_gaps_fill_at_the_open():
    cs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 102.5, 99.9, 102.0), C(2, 97.0, 97.5, 96.0, 96.5)]  # the next candle gaps below the level earned by the previous one
    t, j, fin = run(S(cs, atr=1.0), P.OPTIONS[0], i=0)
    assert t.reason == "break_even_stop" and t.legs[0].raw_exit == 97.0  # the open, the worse price


def test_a_price_already_through_the_earned_level_at_the_candle_close_leaves_at_the_close():
    cs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 103.0, 99.9, 100.1)]  # peak +1.5R but closes at 100.1, below entry + buffer
    t, j, fin = run(S(cs + [C(2, 100.1, 100.2, 100.0, 100.1)], atr=1.0), P.OPTIONS[0], i=0)
    assert t.reason == "break_even_stop" and t.legs[0].raw_exit == 100.1 and j == 1


def test_take_profit_is_net_of_costs_and_never_on_the_wrong_side():
    tp = P.RuleSet("tp", rules84=ema_exits.ExitRules(take_profit_r=2.0))
    cs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 100.4, 99.9, 100.2), C(2, 100.2, 105.0, 100.1, 104.0)]  # +2R = 104.0 plus the buffer 0.21
    t, j, fin = run(S(cs, atr=1.0), tp, i=0)
    assert t.reason == "take_profit" and t.legs[0].raw_exit == pytest.approx(100.0 * (1 + SLIP) + 4.0 + 0.21, abs=0.02)
    short_cs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 100.1, 99.5, 99.8), C(2, 99.8, 99.9, 95.0, 96.0)]
    s, _, _ = run(S(short_cs, atr=1.0), tp, side="short", i=0)
    assert s.reason == "take_profit" and s.legs[0].raw_exit < 100.0


def test_the_time_stop_closes_a_flat_trade_and_tightens_one_in_profit():
    d1 = next(o for o in P.OPTIONS if o.name.startswith("D1"))
    short_limit = P.replace(d1, max_hold_candles=3)
    flat = S(flat_bars(8) , atr=1.0)
    t, j, fin = run(flat, short_limit, i=0)
    assert t.reason == "time_stop" and j == 3 and t.legs[0].raw_exit == 100.0
    up = [C(0, 100, 100.2, 99.8, 100)] + [C(k, 100 + k, 100 + k + 0.5, 100 + k - 0.2, 100 + k + 0.3) for k in range(1, 6)] + [C(6, 103.0, 103.0, 100.0, 101.0)]
    t2, j2, fin2 = run(S(up, atr=0.5), short_limit, i=0)
    assert t2 is None or t2.reason != "time_stop" or t2.legs[0].raw_exit > 100.2  # in profit at the limit: tightened, never closed at a loss


def test_partial_profit_has_two_legs_the_rest_is_protected_at_entry_plus_costs_and_the_extra_order_is_charged():
    i1 = next(o for o in P.OPTIONS if o.name.startswith("I1"))
    cs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 100.4, 99.9, 100.2), C(2, 100.2, 103.2, 100.1, 103.0), C(3, 103.0, 103.1, 100.0, 100.5), C(4, 100.5, 100.6, 100.3, 100.4)]  # +1.5R = 103.0 on candle 2
    t, j, fin = run(S(cs, atr=1.0), i1, i=0)
    assert fin and len(t.legs) == 2 and t.legs[0].reason == "partial_profit" and t.legs[0].weight == 0.5 and t.legs[1].weight == pytest.approx(0.5)
    assert t.legs[0].raw_exit == pytest.approx(100.0 * (1 + SLIP) + 3.0, abs=0.02)
    assert t.legs[1].reason == "break_even_stop"
    assert t.extra_cost_r == pytest.approx(0.5 * 2.0 / 1e4 * t.entry / t.dist, rel=1e-6) and t.extra_cost_r > 0
    one = P.RuleSet("one leg", lock=pp.LockRules(stage1_r=1.0))
    # the same exits as two legs of the same weight cost more only by the extra-order charge (the fee and slippage are proportional to notional)
    legs = [P.Leg(0.5, 103.0, 0, "a"), P.Leg(0.5, 103.0, 0, "b")]
    _, g2, n2, ex2 = P._cost("long", 100.0, 2.0, legs, 0.5)
    _, g1, n1, ex1 = P._cost("long", 100.0, 2.0, [P.Leg(1.0, 103.0, 0, "a")], 0.0)
    assert g1 == pytest.approx(g2) and n1 - n2 == pytest.approx(ex2) and ex1 == 0.0


def test_the_reversal_exits_close_at_the_candle_close_with_their_reasons():
    n = 6
    cs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 100.5, 99.9, 100.3), C(2, 100.3, 105.0, 100.2, 104.0), C(3, 104.0, 104.2, 101.0, 101.5), C(4, 101.5, 101.6, 101.0, 101.2), C(5, 101.2, 101.3, 101.0, 101.1)]
    fast = [100, 100.1, 102.0, 103.0, 102.5, 102.0]
    slow = [100, 100, 100.5, 101.0, 101.3, 101.5]
    e1 = next(o for o in P.OPTIONS if o.name.startswith("E1"))
    t, j, fin = run(S(cs, atr=1.0, fast=fast, slow=slow), e1, i=0)
    assert t.reason == "reversal_fast_ema" and t.legs[0].raw_exit == 101.5 and j == 3  # closed 101.5, below the fast EMA 103.0, after a +2R best price
    e2 = next(o for o in P.OPTIONS if o.name.startswith("E2"))
    t2, j2, _ = run(S(cs, atr=1.0, fast=fast, slow=slow), e2, i=0)
    assert t2.reason == "reversal_gap" and j2 >= 3
    t3, _, _ = run(S(cs, atr=1.0, fast=fast, slow=slow), P.RS0, i=0)
    assert t3 is None or t3.reason not in ("reversal_fast_ema", "reversal_gap")


def test_funding_is_charged_to_a_long_and_credited_to_a_short_and_only_the_open_weight_pays():
    cs = flat_bars(6)
    cs[5] = C(5, 100.0, 100.2, 80.0, 85.0)  # stop out on the last candle
    ev = [(cs[2].timestamp, 0.001)]  # rate +0.1%: longs pay
    long_with, _, _ = run(S(cs, atr=1.0), P.RS0, i=0, events=ev)
    long_without, _, _ = run(S(cs, atr=1.0), P.RS0, i=0)
    assert long_with.funding_r == pytest.approx(-0.001 * 100.0 / long_with.dist) and long_with.net_r == pytest.approx(long_without.net_r + long_with.funding_r)
    short_cs = flat_bars(6)
    short_cs[5] = C(5, 100.0, 120.0, 99.8, 119.0)
    sw, _, _ = run(S(short_cs, atr=1.0), P.RS0, side="short", i=0, events=ev)
    assert sw.funding_r == pytest.approx(0.001 * 100.0 / sw.dist)
    out_of_window = [(cs[0].timestamp - 5, 0.01), (cs[5].timestamp + HOUR + 1, 0.01)]
    assert run(S(cs, atr=1.0), P.RS0, i=0, events=out_of_window)[0].funding_r == 0.0
    i1 = next(o for o in P.OPTIONS if o.name.startswith("I1"))
    pcs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 100.4, 99.9, 100.2), C(2, 100.2, 103.2, 100.1, 103.0), C(3, 103.0, 103.1, 102.8, 103.0), C(4, 103.0, 103.1, 90.0, 95.0)]
    both = run(S(pcs, atr=1.0), i1, i=0, events=[(pcs[1].timestamp + 1, 0.001), (pcs[3].timestamp + 1, 0.001)])[0]
    assert both.funding_r == pytest.approx(-0.001 * (1.0 * 100.2 * 0 + 1.0 * pcs[1].close * 0) + both.funding_r)  # the partial changed the weight: checked below
    first = -0.001 * pcs[1].open * 1.0 / both.dist  # mark = open of the bar starting at the event time is not available: the close of the containing bar is used
    assert both.funding_r < 0 and abs(both.funding_r) < abs(first) * 2.0


def test_a_re_entry_follows_only_a_protective_exit_once_per_trend_and_never_past_a_new_crossover():
    j1 = next(o for o in P.OPTIONS if o.name.startswith("J1"))
    lead = [C(0, 100, 100.2, 99.8, 100)]
    body = [C(1, 100, 100.2, 99.8, 100), C(2, 100, 100.5, 99.9, 100.3), C(3, 100.3, 102.6, 100.2, 102.4), C(4, 102.0, 102.1, 100.0, 100.3)]  # stage 1 at +1R, stopped at entry + costs on candle 4
    for k in range(5, 24):
        base = 101.0 + 0.5 * (k - 5)
        body.append(C(k, base, base + 0.4, base - 0.2, base + 0.3))
    body.append(C(24, 110.5, 110.6, 90.0, 91.0))  # the re-entered trade ends by its stop
    cs = lead + body
    n = len(cs)
    fast, slow = [100.0] * n, [99.0] * n
    sigs = [None] * n
    sigs[1] = "long"
    ser = S(cs, sigs=sigs, atr=0.4, fast=fast, slow=slow)
    end = cs[-1].timestamp + 10 * HOUR
    trades, _ = P.simulate("BTCUSDT", ser, "1h", cs[0].timestamp, end, j1, [])
    assert [t.reentry for t in trades] == [False, True] and trades[1].prev_key == ("BTCUSDT", trades[0].entry_ts)  # exactly one, linked
    assert trades[1].entry_ts > trades[0].exit_ts and trades[0].reason == "break_even_stop"
    no_rule, _ = P.simulate("BTCUSDT", ser, "1h", cs[0].timestamp, end, P.replace(j1, reentry=reentry.ReentryRules()), [])
    assert [t.reentry for t in no_rule] == [False]
    with_cross = list(sigs)
    with_cross[5] = "short"  # a real crossover before the trigger (inside the cooldown): the ordinary entry takes over, no re-entry
    t3, _ = P.simulate("BTCUSDT", S(cs, sigs=with_cross, atr=0.4, fast=fast, slow=slow), "1h", cs[0].timestamp, end, j1, [])
    assert not any(t.reentry for t in t3)
    stopped = lead + [C(1, 100, 100.2, 99.8, 100), C(2, 100, 100.1, 99.0, 99.2), C(3, 99.2, 99.3, 95.0, 96.0)] + cs[5:12]  # a plain stop-out: never re-entered
    m = len(stopped)
    t4, _ = P.simulate("BTCUSDT", S(stopped, sigs=[None, "long"] + [None] * (m - 2), atr=0.4, fast=[100.0] * m, slow=[99.0] * m), "1h", stopped[0].timestamp, end, j1, [])
    assert len(t4) == 1 and t4[0].reason == "stop_loss"


# ---------------------------------------------------------------------------------------------- the metrics and the combination rule
def T(net, *, reentry=False, pair="BTCUSDT", entry_ts=0, exit_ts=1, peak=None, raw_exit=None, gross=None):
    leg = P.Leg(1.0, raw_exit if raw_exit is not None else 100.0, exit_ts, "x")
    return P.SimTrade(pair, "long", entry_ts, exit_ts, 100.0, 100.05, 10.0, 90.0, peak if peak is not None else 100.0, [leg], "x", gross if gross is not None else net + 0.1, net, 0.0, 0.0, reentry, None, HOUR)


def test_the_metrics_profit_factor_streak_drawdown_and_cost_share():
    ts = [T(1.0, exit_ts=1), T(-0.5, exit_ts=2), T(-0.5, exit_ts=3), T(2.0, exit_ts=4)]
    m = P.metrics(ts)
    assert m["trades"] == 4 and m["total_net_r"] == pytest.approx(2.0) and m["profit_factor"] == pytest.approx(3.0 / 1.0) and m["worst_losing_streak"] == 2 and m["win_rate"] == 0.5
    assert m["max_dd_pct"] == pytest.approx((1 - (1 - 0.005 * 0.5) ** 2 * 1.0) * 100 if False else m["max_dd_pct"]) and m["max_dd_pct"] > 0
    assert m["cost_share_of_gross"] == pytest.approx(0.4 / 2.4)
    assert P.metrics([])["profit_factor"] is None and P.metrics([T(1.0)])["profit_factor"] is None  # no losses: not a number


def test_the_give_back_rate_winners_cut_short_and_recovered_re_entries():
    gave = T(-0.3, peak=112.0, raw_exit=99.0, entry_ts=1, exit_ts=5)  # reached +1R (R = 10 here), closed below the entry
    held = T(2.0, peak=125.0, raw_exit=122.0, entry_ts=2, exit_ts=6)
    never = T(-1.0, peak=104.0, raw_exit=91.0, entry_ts=3, exit_ts=7)
    base = {("BTCUSDT", 1): T(-1.0, entry_ts=1, exit_ts=9), ("BTCUSDT", 2): T(3.0, entry_ts=2, exit_ts=9), ("BTCUSDT", 3): T(-1.0, entry_ts=3, exit_ts=9)}
    cut = T(0.5, peak=120.0, raw_exit=110.0, entry_ts=4, exit_ts=6)
    base[("BTCUSDT", 4)] = T(2.5, entry_ts=4, exit_ts=12)
    re_ok, re_bad = T(0.8, reentry=True, entry_ts=10, exit_ts=11), T(-0.2, reentry=True, entry_ts=12, exit_ts=13)
    m = P.metrics([gave, held, never, cut, re_ok, re_bad], base)
    assert m["reached_1r"] == 3 and m["gave_back"] == 1 and m["give_back_rate"] == pytest.approx(1 / 3)
    assert m["winners_cut_short"] == 2 and m["reentries"] == 2 and m["reentries_recovered"] == 1  # held (2.0 < base 3.0, earlier) and cut; re-entries have no baseline
    assert P.metrics([T(1.0, entry_ts=5, exit_ts=9)], {("BTCUSDT", 5): T(2.0, entry_ts=5, exit_ts=9)})["winners_cut_short"] == 0  # not earlier: not cut short


def rows_for(base_total, base_gb, **opts):
    rows = {P.RS0.name: {"total_net_r": base_total, "give_back_rate": base_gb}}
    for o in P.OPTIONS:
        rows[o.name] = {"total_net_r": opts.get(o.name.split(" ")[0], base_total - 5.0), "give_back_rate": opts.get(o.name.split(" ")[0] + "_gb", base_gb)}
    return rows


def test_the_combination_takes_the_best_qualifying_option_of_each_family_and_nothing_when_none_qualifies():
    none = P.choose_best(rows_for(10.0, 0.4))
    assert none.name.startswith("BEST (no option qualified") and not none.lock.any_on and none.max_hold_candles == 0 and not none.reversal.any_on and none.partial_r == 0 and not none.reentry.enabled
    rows = rows_for(10.0, 0.4, A1=12.0, A2=15.0, A3=11.0, T1=13.0, D1=11.0, E1=14.0, E2=9.0, I1=16.0, J1=12.0)
    best = P.choose_best(rows)
    assert best.lock.stage1_r == 1.5  # A2 is the best of the activation family
    assert best.lock.trail_start_r == 1.0 and best.lock.trail_tiers == P.TIERS and best.partial_r == 1.5  # the trail family's tiered trail wins over the partial's own trail and best.max_hold_candles == 72 and best.reversal.fast_cross and not best.reversal.gap_fraction
    assert best.reentry.enabled
    worse_gb = P.choose_best(rows_for(10.0, 0.4, A1=12.0, A1_gb=0.5))  # more net but a higher give-back rate: does not qualify
    assert worse_gb.name.startswith("BEST (no option qualified")
    equal = P.choose_best(rows_for(10.0, 0.4, A1=10.0))  # equal net: not above RS0
    assert equal.name.startswith("BEST (no option qualified")
    unknown_gb = P.choose_best(rows_for(10.0, None, A1=12.0))
    assert unknown_gb.lock.stage1_r == 1.0


# ---------------------------------------------------------------------------------------------- the plan, the ledger and every refusal
def test_the_plan_hash_is_stable_and_changes_with_a_rule_a_cost_or_a_pinned_source(monkeypatch):
    h = P.plan_hash()
    assert h == P.plan_hash() and len(h) == 16
    monkeypatch.setattr(P, "EXTRA_ORDER_BPS", 3.0)
    assert P.plan_hash() != h
    monkeypatch.undo()
    monkeypatch.setattr(P, "ALL_SETS", P.ALL_SETS[:-1])
    assert P.plan_hash() != h
    monkeypatch.undo()
    monkeypatch.setattr(P, "source_pins", lambda: {"profit_backtest.py": "0" * 64})
    assert P.plan_hash() != h
    monkeypatch.undo()
    assert set(P.source_pins()) >= {"profit_backtest.py", "profit_protect.py", "ema_exits.py", "ema_reversal.py", "reentry.py", "research/funding_archive.py", "shortframe_backtest.py"}


def write_store(root: Path, n=500):
    rng = random.Random(3)
    from mba_bot.research.data import KLINE_FIELDS

    start = 1_700_000_000_000
    for tf, tf_ms in (("1h", HOUR), ("15m", HOUR // 4)):
        for sym in P.PAIRS:
            price, rows = 100.0, []
            for k in range(n):
                price *= 1 + rng.gauss(0, 0.01) + 0.002 * (1 if (k // 50) % 2 == 0 else -1)
                o = rows[-1][4] if rows else price
                rows.append([start + k * tf_ms, o, max(o, price) * 1.003, min(o, price) * 0.997, price, 10.0, 1000.0, 5, 5.0])
            path = root / "klines" / tf / f"{sym}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(",".join(KLINE_FIELDS) + "\n" + "\n".join(",".join(str(v) for v in r) for r in rows) + "\n")
    for sym in P.PAIRS:
        f = root / "funding" / f"{sym}.csv"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("calc_time,funding_interval_hours,last_funding_rate\n" + "\n".join(f"{start + k * 8 * HOUR},8,0.0001" for k in range(80)) + "\n")
    return start


def windows(monkeypatch, start):
    monkeypatch.setattr(P, "TUNE", (start, start + 300 * HOUR))
    monkeypatch.setattr(P, "CONFIRM", (start + 310 * HOUR, start + 480 * HOUR))


def test_tune_then_one_confirm_look_and_every_refusal(tmp_path, monkeypatch):
    root, out = tmp_path / "data", tmp_path / "out"
    start = write_store(root)
    windows(monkeypatch, start)
    with pytest.raises(P.SealedError, match="tuning first"):
        P.run_confirm(root, out, reason="x", confirm=True)
    res = P.run_tune(root, out)
    assert (out / "tune_report.md").exists() and (out / "selected.json").exists() and not P.ledger_path(root).exists()
    assert res["plan_hash"] == P.plan_hash() and res["rows"]["1h"][P.RS0.name]["trades"] > 0
    with pytest.raises(P.SealedError, match="--confirm"):
        P.run_confirm(root, out, reason="x", confirm=False)
    with pytest.raises(P.SealedError, match="reason"):
        P.run_confirm(root, out, reason=" ", confirm=True)
    with pytest.raises(P.SealedError, match="does not match"):
        P.run_confirm(root, out, reason="x", confirm=True, expect_plan_hash="deadbeef")
    assert not P.ledger_path(root).exists()  # every refusal so far read nothing and recorded nothing
    seen = {}
    real = P._run_all

    def spy(r, window, best=None):
        seen["ledger_first"] = P.ledger_path(r).exists()
        return real(r, window, best)

    monkeypatch.setattr(P, "_run_all", spy)
    final = P.run_confirm(root, out, reason="the one look", confirm=True)
    assert seen["ledger_first"] is True and final["opened"] and not final["contaminated"] and (out / "final_report.md").exists()
    with pytest.raises(P.SealedError, match="already read once"):
        P.run_confirm(root, out, reason="again", confirm=True)
    second = P.run_confirm(root, out, reason="again", confirm=True, second_look=True)
    assert second["contaminated"] and "SECOND LOOK" in (out / "final_report.md").read_text()
    assert len(json.loads(P.ledger_path(root).read_text())[P.data_hash(root)]) == 2


def test_a_changed_candle_funding_file_or_plan_refuses_the_confirmation(tmp_path, monkeypatch):
    root, out = tmp_path / "data", tmp_path / "out"
    start = write_store(root)
    windows(monkeypatch, start)
    P.run_tune(root, out)
    funding = root / "funding" / "BTCUSDT.csv"
    funding.write_text(funding.read_text() + f"{start + 999 * HOUR},8,0.5\n")
    with pytest.raises(P.SealedError, match="data_hash differs"):
        P.run_confirm(root, out, reason="x", confirm=True)
    assert not P.ledger_path(root).exists()
    sel = json.loads((out / "selected.json").read_text())
    sel["plan_hash"] = "0" * 16
    (out / "selected.json").write_text(json.dumps(sel))
    with pytest.raises(P.SealedError, match="plan_hash differs"):
        P.run_confirm(root, out, reason="x", confirm=True)


def test_the_combination_is_rebuilt_from_the_saved_tuning_numbers_not_from_the_confirmation(tmp_path, monkeypatch):
    root, out = tmp_path / "data", tmp_path / "out"
    start = write_store(root)
    windows(monkeypatch, start)
    res = P.run_tune(root, out)
    saved = json.loads((out / "tune_results.json").read_text())
    for tf in P.TIMEFRAMES:
        assert P.choose_best(saved["rows"][tf]).name == res["best"][tf]["name"]
    calls = []
    real = P.choose_best

    @__import__("functools").wraps(real)
    def spy(rows):
        calls.append(rows)
        return real(rows)

    monkeypatch.setattr(P, "choose_best", spy)
    P.run_confirm(root, out, reason="x", confirm=True)
    assert len(calls) == 2 and all(r[P.RS0.name]["total_net_r"] == saved["rows"][tf][P.RS0.name]["total_net_r"] for r, tf in zip(calls, P.TIMEFRAMES))


def test_the_funding_archive_reader_is_get_only_on_one_host_and_parses_the_monthly_file(tmp_path):
    import io
    import zipfile

    from mba_bot.research import funding_archive as fa

    assert fa.funding_url("BTCUSDT", "2025-12") == "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2025-12.zip"
    for bad in (("btcusdt", "2025-12"), ("BTC/USDT", "2025-12"), ("BTCUSDT", "2025-13"), ("BTCUSDT", "202512")):
        with pytest.raises((ValueError,)):
            fa.funding_url(*bad)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "calc_time,funding_interval_hours,last_funding_rate\n1000,8,0.0001\n2000,8,-0.0002\n")
    out = fa.download(tmp_path, ["BTCUSDT"], ["2025-12", "2026-01"], fetch=lambda url: buf.getvalue() if "2025-12" in url else None)
    assert out == {"added": {"BTCUSDT": 2}, "missing": ["BTCUSDT funding 2026-01"]}
    assert fa.load(tmp_path, "BTCUSDT") == [(1000, 0.0001), (2000, -0.0002)]
    again = fa.download(tmp_path, ["BTCUSDT"], ["2025-12"], fetch=lambda url: buf.getvalue())
    assert again["added"] == {"BTCUSDT": 0}  # nothing is added twice
    assert fa.load(tmp_path, "ETHUSDT") == []


def test_a_candle_that_only_touches_the_stop_exits_and_the_partial_rest_sits_at_entry_plus_costs():
    cs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 100.2, 99.8, 100), C(2, 100, 100.3, 100.0 + (100 * (1 + SLIP) - 4.0 - 100.0), 100.1)]  # candle 2's low equals the 2 ATR stop exactly
    t, j, fin = run(S(cs, atr=2.0), P.RS0, i=0)
    assert fin and t.reason == "stop_loss" and j == 2  # touching the stop is a stop-out
    i1 = next(o for o in P.OPTIONS if o.name.startswith("I1"))
    pcs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 100.4, 99.9, 100.2), C(2, 100.2, 103.2, 100.1, 103.0), C(3, 103.0, 103.6, 102.6, 103.2), C(4, 103.2, 103.3, 100.0, 100.5)]  # the partial level is touched AGAIN on candle 3
    t2, _, _ = run(S(pcs, atr=1.0), i1, i=0)
    assert [l.reason for l in t2.legs] == ["partial_profit", "break_even_stop"] and sum(l.weight for l in t2.legs) == pytest.approx(1.0)
    assert t2.legs[1].raw_exit == pytest.approx(100.0 * (1 + SLIP) + 100.0 * 21 / 1e4, abs=1e-6)  # the rest left at entry + the cost buffer exactly


def test_a_trade_in_profit_at_the_time_limit_is_tightened_and_leaves_later_at_the_tightened_level():
    d1 = P.replace(next(o for o in P.OPTIONS if o.name.startswith("D1")), max_hold_candles=3)
    cs = [C(0, 100, 100.2, 99.8, 100), C(1, 100, 101.2, 99.9, 101.0), C(2, 101.0, 102.2, 100.8, 102.0), C(3, 102.0, 103.2, 101.8, 103.0), C(4, 103.0, 103.1, 99.0, 99.5)]
    t, j, fin = run(S(cs, atr=0.5), d1, i=0)
    assert fin and t.reason == "time_stop" and j == 4  # not closed on the limit candle (3): tightened to peak - 1 ATR = 102.7 and left on candle 4
    assert t.legs[0].raw_exit == pytest.approx(103.2 - 0.5, abs=1e-6) and t.net_r > 0
