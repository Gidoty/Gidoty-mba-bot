"""The entry-rule study (src/mba_bot/entry_study.py). Research only.

PROVES: with no candidate rule the simulators reproduce the pre-registered EMA, Donchian and RSI simulations trade for trade; every filter and guard at its edge; that the higher-timeframe trend never reads a bar that has
not closed; the stateful guards; the pre-registered pass rule (each part on its own, both ways); the month-block difference interval; the window geometry (nine non-overlapping test windows, each tune window before its test
window and no overlap); the pair-by-trailing-performance rule reads nothing from the future; and a full run on synthetic data end to end.
DOES NOT PROVE: that any entry rule has an edge. The numbers it produces are measurements."""
from __future__ import annotations

import random

import pytest

from mba_bot import entry_study as E
from mba_bot import profit_backtest as P
from mba_bot import shortframe_backtest as SF
from mba_bot.strategy import Candle

HOUR = 3_600_000
BASE = 1_700_000_000_000 // HOUR * HOUR


def C(i, o, h, lo, c, v=1.0, base=BASE):
    return Candle(base + i * HOUR, o, h, lo, c, v)


def walk(n=3000, seed=5, trend=True):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for i in range(n):
        drift = (0.004 * (1 if (i // 120) % 2 == 0 else -1)) if trend else (100.0 - price) * 0.0005
        o = price
        price *= 1 + rng.gauss(drift, 0.01)
        rows.append(C(i, o, max(o, price) * (1 + abs(rng.gauss(0, 0.003))), min(o, price) * (1 - abs(rng.gauss(0, 0.003))), price, 1.0 + (3.0 if rng.random() < 0.08 else 0.0) + rng.random()))
    return rows


def ranging(n=3000, seed=9):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for i in range(n):
        o = price
        price += (100.0 - price) * 0.05 + rng.gauss(0, 0.9)
        rows.append(C(i, o, max(o, price) + abs(rng.gauss(0, 0.3)), min(o, price) - abs(rng.gauss(0, 0.3)), price, 1.0 + rng.random()))
    return rows


# ---------------------------------------------------------------------------------------------- the unfiltered simulators reproduce the pre-registered ones
@pytest.mark.parametrize("strat,series,sf_sim,min_trades", [("ema", walk, SF.simulate_ema, 15), ("donchian", walk, SF.simulate_donchian, 15), ("rsi", ranging, SF.simulate_rsi, 5)])
def test_with_no_rule_each_simulator_reproduces_the_pre_registered_one(strat, series, sf_sim, min_trades):
    cs = series()
    f = E.build_feat(cs)
    start, end = cs[0].timestamp, cs[-1].timestamp + HOUR
    want, _ = sf_sim("X", cs, "1h", start, end)
    got = E.SIM[strat]("X", f, None, start, end, [])
    assert len(want) >= min_trades and len(got) == len(want)
    for a, b in zip(want, got):
        assert (a.entry_ts, a.exit_ts, a.reason) == (b.entry_ts, b.exit_ts, b.reason) and a.net_r == pytest.approx(b.net_r, abs=1e-9)


# ---------------------------------------------------------------------------------------------- the filters
def feat(**kw):
    n = 10
    cs = [C(i, 100, 101, 99, 100, 2.0) for i in range(n)]
    f = E.Feat(cs, [1.0] * n, [25.0] * n, [100.5] * n, [100.0] * n, [2.0] * n, [0.01] * n, [0.005] * n, [1] * n, [-1] * n)
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def test_each_stateless_filter_at_its_edge_and_a_missing_value_never_passes():
    n = 10
    assert E.passes("gap", feat(), 5, "long") and not E.passes("gap", feat(fast=[100.2] * n), 5, "long")  # |gap| 0.2 ATR < 0.25
    assert E.passes("gap", feat(fast=[100.25] * n), 5, "long") and not E.passes("gap", feat(atr=[None] * n), 5, "long")
    assert E.passes("htf4", feat(), 5, "long") and not E.passes("htf4", feat(), 5, "short") and E.passes("htfD", feat(), 5, "short") and not E.passes("htfD", feat(), 5, "long")
    assert not E.passes("htf4", feat(htf4=[0] * n), 5, "long")  # no trend yet: nothing aligned
    assert E.passes("adx", feat(), 5, "long") and not E.passes("adx", feat(adx=[19.99] * n), 5, "long") and E.passes("adx", feat(adx=[20.0] * n), 5, "long") and not E.passes("adx", feat(adx=[None] * n), 5, "long")
    assert E.passes("vol", feat(), 5, "long") and not E.passes("vol", feat(vol_avg=[2.01] * n), 5, "long") and not E.passes("vol", feat(vol_avg=[None] * n), 5, "long")
    assert E.passes("volat", feat(), 5, "long") and not E.passes("volat", feat(atr_med=[0.0101] * n), 5, "long") and not E.passes("volat", feat(atr_med=[None] * n), 5, "long")
    assert E.passes("fee3", feat(atr_pct=[0.0063] * n), 5, "long") and not E.passes("fee3", feat(atr_pct=[0.00629] * n), 5, "long")  # 3 x 21 bps = 63 bps


def test_the_higher_timeframe_trend_reads_only_completed_bars():
    cs = [C(i, 100 + i, 101 + i, 99 + i, 100.0 + i, base=0) for i in range(24 * 80)]
    t4 = E._htf_trend(cs, 4 * HOUR, 20, 50)
    assert t4[:4 * 50 - 1] == [0] * (4 * 50 - 1)  # fewer than 50 completed 4h bars: no trend
    assert t4[-1] == 1
    # the value at a 1h candle cannot change until a group completes: it is constant within a group
    assert all(t4[i] == t4[i - 1] for i in range(4 * 60, 4 * 60 + 3))
    spike = list(cs)
    spike[-1] = C(len(cs) - 1, 100, 100, 1, 1, base=0)  # a crash inside the LAST (completing) candle
    assert E._htf_trend(spike, 4 * HOUR, 20, 50)[-2] == t4[-2]  # the bar before it is unaffected: no look-ahead
    down = [C(i, 1000 - i, 1001 - i, 999 - i, 1000.0 - i, base=0) for i in range(24 * 80)]
    assert E._htf_trend(down, E.DAY, 20, 50)[-1] == -1


def test_the_stateful_guards_cool_after_a_stop_and_pause_after_three_losses():
    class T:
        def __init__(self, reason, net, exit_ts):
            self.reason, self.net_r, self.exit_ts = reason, net, exit_ts

    st = E.State()
    E.record("cool24", st, T("stop_loss", -1.0, 1000 * HOUR))
    assert E.blocked_by_guard("cool24", st, 1000 * HOUR + 1 * HOUR) and E.blocked_by_guard("cool24", st, 1001 * HOUR + 23 * HOUR - 1) and not E.blocked_by_guard("cool24", st, 1001 * HOUR + 24 * HOUR)
    st2 = E.State()
    E.record("cool24", st2, T("crossover_exit", -1.0, 1000 * HOUR))
    assert not E.blocked_by_guard("cool24", st2, 1002 * HOUR)  # only a plain stop-out starts the cooldown
    s3 = E.State()
    for k in range(2):
        E.record("streak3", s3, T("crossover_exit", -0.1, (1000 + k) * HOUR))
    assert not E.blocked_by_guard("streak3", s3, 1100 * HOUR)
    E.record("streak3", s3, T("crossover_exit", -0.1, 1002 * HOUR))
    assert E.blocked_by_guard("streak3", s3, 1003 * HOUR) and not E.blocked_by_guard("streak3", s3, 1003 * HOUR + 24 * HOUR)
    s4 = E.State()
    for net in (-0.1, -0.1, 0.5, -0.1):
        E.record("streak3", s4, T("crossover_exit", net, 1000 * HOUR))
    assert not E.blocked_by_guard("streak3", s4, 1001 * HOUR)  # a winner resets the count


def test_a_guard_changes_the_trades_a_stateless_filter_cannot(monkeypatch):
    monkeypatch.setitem(E.FILTER_PARAMS, "cool_hours", 400)  # a long cooldown so the synthetic series has entries to refuse
    cs = walk()
    f = E.build_feat(cs)
    start, end = cs[0].timestamp, cs[-1].timestamp + HOUR
    base = E.sim_ema("X", f, None, start, end, [])
    cool = E.sim_ema("X", f, "cool24", start, end, [])
    assert 0 < len(cool) < len(base)
    stops = [t for t in cool if t.reason == "stop_loss"]
    ts = sorted(cool, key=lambda t: t.entry_ts)
    for a, b in zip(ts, ts[1:]):
        if a.reason == "stop_loss":
            assert b.entry_ts - a.exit_ts >= 400 * HOUR  # no entry inside the cooldown after a stop-out
    assert stops


# ---------------------------------------------------------------------------------------------- the pass rule
def T(net, entry_ts, pair="BTCUSDT"):
    return P.SimTrade(pair, "long", entry_ts, entry_ts + HOUR, 100.0, 100.0, 10.0, 90.0, 100.0, [P.Leg(1.0, 100.0, entry_ts, "x")], "x", net, net, 0.0, 0.0, False, None, HOUR)


def spread(values_per_window, per_window_n):
    out = []
    for k, s in enumerate(E.TEST_STARTS):
        for i in range(per_window_n):
            out.append(T(values_per_window[k][i % len(values_per_window[k])], s + (i * 7 % 150) * 24 * HOUR // 2 + i))
    return out


def test_the_pass_rule_each_part_both_ways(monkeypatch):
    monkeypatch.setattr(E, "BOOT", {**E.BOOT, "reps": 200})
    good = spread([[0.4, 0.2, 0.3, 0.1]] * 9, 40)  # 360 trades, all positive
    base = spread([[0.0, -0.2, 0.1, -0.1]] * 9, 40)
    e = E.evaluate(good, base)
    assert e["P1"] and e["P2"] and e["P3"] and e["P4"] and e["passes_all"] and e["positive_windows"] == 9
    few = E.evaluate(good[:100], base)
    assert not few["P1"] and not few["passes_all"]
    mixed = spread([[0.4, 0.3]] * 4 + [[-0.9, -0.8]] * 5, 40)
    m = E.evaluate(mixed, base)
    assert m["positive_windows"] == 4 and not m["P3"] and not m["edge_survives"]
    bad = spread([[-0.1, -0.2]] * 9, 40)
    assert not E.evaluate(bad, base)["P2"]
    same = E.evaluate(base, base)
    assert not same["P4"] and not same["passes_all"]  # a rule that adds nothing over the unfiltered strategy
    seven = spread([[0.4, 0.3]] * 7 + [[-0.3, -0.2]] * 2, 40)
    assert E.evaluate(seven, base)["P3"] and E.evaluate(seven, base)["positive_windows"] == 7  # exactly 7 of 9 passes the stability part


def test_trades_outside_every_test_window_are_ignored():
    outside = [T(5.0, E.TEST_STARTS[0] - 10 * 24 * HOUR), T(5.0, E.TEST_STARTS[-1] + E.TEST_LEN + 24 * HOUR)]
    assert E.evaluate(outside, outside)["trades"] == 0


def test_the_month_block_difference_interval():
    a = [(f"2024-{m:02d}", 1.0) for m in range(1, 11) for _ in range(5)]
    b = [(f"2024-{m:02d}", 0.0) for m in range(1, 11) for _ in range(5)]
    d = E.block_mean_diff(a, b, reps=300, level=0.99, seed=1)
    assert d[0] == pytest.approx(1.0) and d[1] > 0.9 and d[2] <= 1.0 + 1e-9
    assert E.block_mean_diff(a[:5], b[:5], reps=100, level=0.99, seed=1) is None and E.block_mean_diff([], b, reps=100, level=0.99, seed=1) is None
    noisy = [(f"2024-{m:02d}", (1.0 if m % 2 else -1.0)) for m in range(1, 11) for _ in range(5)]
    n = E.block_mean_diff(noisy, b, reps=300, level=0.99, seed=1)
    assert n[1] < 0 < n[2]


# ---------------------------------------------------------------------------------------------- the geometry and the whole run
def test_the_nine_test_windows_do_not_overlap_each_tune_window_precedes_its_test_and_the_run_covers_them():
    assert len(E.TEST_STARTS) == E.PASS["windows"] == 9
    for a, b in zip(E.TEST_STARTS, E.TEST_STARTS[1:]):
        assert a + E.TEST_LEN <= b
    for s in E.TEST_STARTS:
        assert E.RUN_START <= s - E.TUNE_LEN or s - E.TUNE_LEN >= E.RUN_START - 40 * 24 * HOUR  # the tune window starts at or near the start of the data
    assert E.TEST_STARTS[-1] + E.TEST_LEN <= E.RUN_END + 24 * HOUR


def test_a_full_run_on_synthetic_data_end_to_end_and_the_trailing_pair_rule_reads_nothing_from_the_future(tmp_path, monkeypatch):
    from mba_bot.research.data import KLINE_FIELDS

    n = 9 * 400 + 1200
    start = BASE
    root = tmp_path / "data"
    for k, sym in enumerate(P.PAIRS):
        cs = walk(n, seed=40 + k)
        path = root / "klines" / "1h" / f"{sym}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(",".join(KLINE_FIELDS) + "\n" + "\n".join(f"{c.timestamp},{c.open},{c.high},{c.low},{c.close},{c.volume},1000,5,5" for c in cs) + "\n")
        f = root / "funding" / f"{sym}.csv"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("calc_time,funding_interval_hours,last_funding_rate\n" + "\n".join(f"{start + i * 8 * HOUR},8,0.0001" for i in range(n // 8 + 1)) + "\n")
    monkeypatch.setattr(E, "TEST_LEN", 380 * HOUR)
    monkeypatch.setattr(E, "TUNE_LEN", 600 * HOUR)
    monkeypatch.setattr(E, "TEST_STARTS", [start + 1250 * HOUR + k * 400 * HOUR for k in range(9)])
    monkeypatch.setattr(E, "RUN_START", start + 100 * HOUR)
    monkeypatch.setattr(E, "RUN_END", start + n * HOUR)
    monkeypatch.setattr(E, "BOOT", {**E.BOOT, "reps": 40})
    monkeypatch.setitem(E.FILTER_PARAMS, "pair_trailing_days", 20)
    res = E.run_all(root)
    for strat in E.STRATEGIES:
        evals = res[strat]["evals"]
        assert {"none", "pair:trailing_best", "WALK_FORWARD_PICK"} <= set(evals) and all(f"pair:{p}" in evals for p in P.PAIRS)
        assert all(c in evals for c in E.CANDIDATES[strat]) and len(res[strat]["picks"]) == 9
        assert evals["none"]["trades"] > 0
    ema = res["ema"]["evals"]
    assert ema["none"]["trades"] >= ema["fee3"]["trades"] and ema["none"]["trades"] >= ema["htf4"]["trades"]  # a filter never adds trades
    text = E.report(res)
    assert "walk-forward" in text.lower() and "passes all" in text


def test_the_trailing_pair_rule_reads_nothing_from_the_future_and_breaks_ties_by_name(monkeypatch):
    s0 = 1_000_000 * HOUR
    d = 24 * HOUR
    old = [T(1.0, s0 - 100 * d, "ETHUSDT"), T(0.5, s0 - 50 * d, "BTCUSDT")]
    assert E.trailing_best_pair(old, s0) == "ETHUSDT"
    future = old + [T(50.0, s0 + 1 * d, "BTCUSDT"), T(50.0, s0 - 1 * HOUR, "BTCUSDT")]  # a huge BTC win that exits at or after the window start is invisible; one entered before but exiting after it too
    future[-1].exit_ts = s0 + 10 * HOUR
    assert E.trailing_best_pair(future, s0) == "ETHUSDT"
    ancient = old + [T(100.0, s0 - 400 * d, "BNBUSDT")]  # older than 365 days: not read either
    assert E.trailing_best_pair(ancient, s0) == "ETHUSDT"
    assert E.trailing_best_pair([], s0) == max(P.PAIRS)  # no information: the last pair by name, deterministically


def test_the_plan_hash_is_stable_and_changes_with_a_rule_a_window_or_a_source(monkeypatch):
    h = E.plan_hash()
    assert h == E.plan_hash() and len(h) == 16
    monkeypatch.setitem(E.FILTER_PARAMS, "adx_min", 21.0)
    assert E.plan_hash() != h
    monkeypatch.undo()
    monkeypatch.setitem(E.PASS, "min_trades", 301)
    assert E.plan_hash() != h
    monkeypatch.undo()
    monkeypatch.setattr(E, "TEST_STARTS", E.TEST_STARTS[:-1])
    assert E.plan_hash() != h
    monkeypatch.undo()
    assert set(E.source_pins()) >= {"entry_study.py", "profit_backtest_dr.py", "research/bootstrap.py", "donchian.py", "rsi_mr.py"}


def test_six_positive_windows_of_nine_is_not_stable_enough_and_a_mean_that_is_not_distinguishable_from_zero_fails_p2(monkeypatch):
    monkeypatch.setattr(E, "BOOT", {**E.BOOT, "reps": 200})
    base = spread([[0.0, -0.2, 0.1, -0.1]] * 9, 40)
    six = spread([[0.4, 0.3]] * 6 + [[-0.1, -0.2]] * 3, 40)
    e = E.evaluate(six, base)
    assert e["positive_windows"] == 6 and not e["P3"] and not e["edge_survives"]
    noisy = spread([[1.0, 0.9]] * 5 + [[-0.95, -0.9]] * 4, 40)
    n = E.evaluate(noisy, base)
    assert n["mean_net_r"] > 0 and not n["P2"]  # a positive mean whose interval reaches below zero is not an edge
    cand = spread([[1.0]] * 5 + [[-0.9]] * 4, 40)
    quiet_base = spread([[0.0]] * 9, 40)
    c = E.evaluate(cand, quiet_base)
    assert c["diff_vs_unfiltered"][0] > 0 and not c["P4"]  # better on average, but not distinguishable from the unfiltered strategy


def test_a_trade_belongs_to_the_window_it_was_entered_in_even_if_it_exits_after_it():
    s = E.TEST_STARTS[0]
    t = T(1.0, s + E.TEST_LEN - HOUR // 2)
    t.exit_ts = s + E.TEST_LEN + 5 * HOUR
    assert E.evaluate([t], [t])["trades"] == 1


def test_the_donchian_cooldown_after_a_stop_is_honoured_by_the_study_simulator(monkeypatch):
    monkeypatch.setattr(SF, "DONCHIAN_COOLDOWN_MS", 300 * HOUR)
    cs = walk()
    f = E.build_feat(cs)
    start, end = cs[0].timestamp, cs[-1].timestamp + HOUR
    want, _ = SF.simulate_donchian("X", cs, "1h", start, end)
    got = E.sim_donchian("X", f, None, start, end, [])
    assert len(got) == len(want) and [(t.entry_ts, t.exit_ts) for t in got] == [(t.entry_ts, t.exit_ts) for t in want]
    monkeypatch.setattr(SF, "DONCHIAN_COOLDOWN_MS", 0)
    assert len(E.sim_donchian("X", f, None, start, end, [])) > len(got)  # without the cooldown there are more entries: it bites
