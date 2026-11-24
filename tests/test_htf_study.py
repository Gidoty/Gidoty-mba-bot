"""The higher-timeframe study (src/mba_bot/htf_study.py). Research only.

PROVES: 4h and daily candles are built from complete groups of 1h candles only; at 1h the parametrised simulators reproduce the entry study's simulators trade for trade; the EMA signal with the shipped periods equals the
pre-registered one; the development and confirmation rules (each part both ways, windows with no trades never count as positive); trades per year; that the development run neither counts nor evaluates a trade from the
confirmation windows; that nothing passing development leaves the confirmation windows unread and unrecorded; the one-look ledger (written before the read) and every refusal.
DOES NOT PROVE anything about profit."""
from __future__ import annotations

import json
import random

import pytest

from mba_bot import entry_study as E
from mba_bot import htf_study as H
from mba_bot import profit_backtest as P
from mba_bot import shortframe_backtest as SF
from mba_bot.strategy import Candle

HOUR = 3_600_000
DAY = 24 * HOUR
BASE = 1_700_000_000_000 // DAY * DAY


def C(i, o, h, lo, c, v=1.0, base=BASE):
    return Candle(base + i * HOUR, o, h, lo, c, v)


def walk(n=3000, seed=5):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for i in range(n):
        o = price
        price *= 1 + rng.gauss(0.004 * (1 if (i // 120) % 2 == 0 else -1), 0.01)
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


def test_aggregation_builds_complete_groups_only_with_the_right_ohlcv():
    cs = [C(i, 100 + i, 110 + i, 90 + i, 101 + i, 2.0) for i in range(24 * 3)]
    d = H.aggregate(cs, DAY)
    assert len(d) == 3 and d[0].open == 100 and d[0].close == 101 + 23 and d[0].high == 110 + 23 and d[0].low == 90 and d[0].volume == 48.0 and d[1].timestamp == BASE + DAY
    h4 = H.aggregate(cs, 4 * HOUR)
    assert len(h4) == 18 and h4[1].open == 104 and h4[1].close == 101 + 7 and h4[1].high == 110 + 7 and h4[1].low == 90 + 4
    gap = cs[:30] + cs[31:]  # one missing 1h candle inside the second day: that day (and its 4h group) is dropped, never invented
    assert len(H.aggregate(gap, DAY)) == 2 and len(H.aggregate(gap, 4 * HOUR)) == 17
    partial = cs[:24 + 5]
    assert len(H.aggregate(partial, DAY)) == 1  # the unfinished day is not a candle


def test_the_ema_signal_with_the_shipped_periods_equals_the_pre_registered_one():
    cs = walk()
    assert H.ema_signals(cs, 20, 50)[0] == SF.ema_signal_series(cs)[0]
    assert H.ema_signals(cs, 12, 26)[0] != H.ema_signals(cs, 20, 50)[0]


@pytest.mark.parametrize("strat,series,params,ref,min_trades", [("ema", walk, (20, 50), E.sim_ema, 15), ("donchian", walk, (20, 10), E.sim_donchian, 15), ("rsi", ranging, (), E.sim_rsi, 5)])
def test_at_1h_with_the_shipped_parameters_each_simulator_reproduces_the_entry_study_simulator(strat, series, params, ref, min_trades):
    cs = series()
    start, end = cs[0].timestamp, cs[-1].timestamp + HOUR
    want = ref("X", E.build_feat(cs), None, start, end, [])
    got = H.SIM[strat]("X", cs, HOUR, params, start, end, [])
    assert len(want) >= min_trades and len(got) == len(want)
    for a, b in zip(want, got):
        assert (a.entry_ts, a.exit_ts, a.reason) == (b.entry_ts, b.exit_ts, b.reason) and a.net_r == pytest.approx(b.net_r, abs=1e-9)


def T(net, entry_ts, pair="BTCUSDT", dist=10.0, entry=100.0):
    return P.SimTrade(pair, "long", entry_ts, entry_ts + DAY, entry, entry, dist, entry - dist, entry, [P.Leg(1.0, entry, entry_ts, "x")], "x", net + 0.2, net, 0.0, 0.0, False, None, DAY)


def windowed(values_per_window, n, windows):
    out = []
    for k in windows:
        s = E.TEST_STARTS[k]
        for i in range(n):
            out.append(T(values_per_window[k % len(values_per_window)][i % len(values_per_window[k % len(values_per_window)])], s + i * 3 * DAY))
    return out


def test_the_development_rule_each_part_both_ways(monkeypatch):
    monkeypatch.setattr(H, "BOOT", {**H.BOOT, "reps": 200})
    dev = H.DEV_WINDOWS
    good = windowed([[0.5, 0.3, 0.4]], 10, dev)  # 60 trades, all positive
    e = H.evaluate(good, dev, 40, 5)
    assert e["P1"] and e["P2"] and e["P3"] and H.dev_pass(e) and e["positive_windows"] == 6 and e["avg_stop_pct"] == pytest.approx(10.0) and e["avg_cost_r"] == pytest.approx(0.2)
    assert not H.evaluate(good[:30], dev, 40, 5)["P1"]
    five = windowed([[0.5, 0.4]] * 5 + [[-0.9, -0.8]], 10, dev)
    f = H.evaluate(five, dev, 40, 5)
    assert f["positive_windows"] == 5 and f["P3"]
    four = windowed([[0.5, 0.4]] * 4 + [[-0.9, -0.8]] * 2, 10, dev)
    assert not H.evaluate(four, dev, 40, 5)["P3"] and not H.dev_pass(H.evaluate(four, dev, 40, 5))
    noisy = windowed([[1.0, 0.9]] * 3 + [[-0.95, -0.9]] * 3, 10, dev)
    n = H.evaluate(noisy, dev, 40, 5)
    assert not n["P2"]  # positive or negative sign alone does not pass: the interval must exclude zero
    sparse = windowed([[0.5]], 10, (0, 1, 2, 3))  # trades in only 4 of the 6 windows: the empty windows are not positive
    assert H.evaluate(sparse, dev, 40, 5)["positive_windows"] == 4 and not H.evaluate(sparse, dev, 40, 5)["P3"]


def test_trades_outside_the_chosen_windows_are_ignored_and_the_confirmation_rule_is_its_own():
    in_confirm = windowed([[0.5, 0.4]], 10, H.CONFIRM_WINDOWS)
    assert H.evaluate(in_confirm, H.DEV_WINDOWS, 1, 1)["trades"] == 0
    c = H.evaluate(in_confirm, H.CONFIRM_WINDOWS, 15, 2)
    assert c["trades"] == 30 and c["P1"] and c["P3"] and c["positive_windows"] == 3


def test_trades_per_year():
    start = E.RUN_START
    trades = [T(0.1, start + k * 10 * DAY) for k in range(73)]
    t = H.trades_per_year(trades, start, start + 730 * DAY)
    assert t["years"] == pytest.approx(730 / 365.25) and t["pooled"] == pytest.approx(73 / (730 / 365.25)) and t["per_pair_avg"] == pytest.approx(t["pooled"] / 4) and t["per_pair"]["BTCUSDT"] == pytest.approx(t["pooled"])
    assert H.trades_per_year(trades, start, start + 5 * DAY)["pooled"] == pytest.approx(1 / (5 / 365.25))  # only trades inside the span


def make_root(tmp_path, monkeypatch):
    from mba_bot.research.data import KLINE_FIELDS

    n = 24 * 560
    start = BASE
    root = tmp_path / "data"
    for k, sym in enumerate(P.PAIRS):
        cs = walk(n, seed=70 + k)
        path = root / "klines" / "1h" / f"{sym}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(",".join(KLINE_FIELDS) + "\n" + "\n".join(f"{c.timestamp},{c.open},{c.high},{c.low},{c.close},{c.volume},1000,5,5" for c in cs) + "\n")
        f = root / "funding" / f"{sym}.csv"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("calc_time,funding_interval_hours,last_funding_rate\n" + "\n".join(f"{start + i * 8 * HOUR},8,0.0001" for i in range(n // 8 + 1)) + "\n")
    monkeypatch.setattr(E, "RUN_START", start + 100 * DAY)
    monkeypatch.setattr(E, "RUN_END", start + 560 * DAY)
    monkeypatch.setattr(E, "TEST_LEN", 40 * DAY)
    monkeypatch.setattr(E, "TEST_STARTS", [start + 160 * DAY + k * 45 * DAY for k in range(9)])
    monkeypatch.setattr(H, "BOOT", {**H.BOOT, "reps": 40})
    return root


def test_the_development_run_never_counts_or_evaluates_a_confirmation_trade(tmp_path, monkeypatch):
    root = make_root(tmp_path, monkeypatch)
    dev_end = E.TEST_STARTS[6]
    crafted = {}
    for cell in H.CELLS:
        crafted[cell] = [T(0.4, E.TEST_STARTS[i % 6] + (i // 6 + 1) * 3 * DAY, pair=H.PAIRS[i % 4]) for i in range(30)] + [T(50.0, dev_end + 3 * DAY + i, pair="BTCUSDT") for i in range(200)]  # a huge win inside the confirmation windows
    monkeypatch.setattr(H, "simulate_cells", lambda r, cells=H.CELLS: crafted)
    out = tmp_path / "out"
    res = H.run_dev(root, out)
    for cell, e in res["rows"].items():
        assert e["trades"] == 30 and e["total_net_r"] == pytest.approx(30 * 0.4)  # none of the 200 confirmation-window trades
    assert "50.0" not in (out / "dev_results.json").read_text() and "+50" not in (out / "dev_report.md").read_text()
    saved = json.loads((out / "dev_results.json").read_text())
    assert all(v["trades"] == 30 for v in saved["rows"].values())


def test_nothing_passing_development_leaves_the_confirmation_unread_and_unrecorded(tmp_path, monkeypatch):
    root = make_root(tmp_path, monkeypatch)
    monkeypatch.setattr(H, "simulate_cells", lambda r, cells=H.CELLS: {c: [T(-0.2, E.TEST_STARTS[0] + i * 2 * DAY) for i in range(60)] for c in H.CELLS})
    out = tmp_path / "out"
    res = H.run_dev(root, out)
    assert res["passing"] == [] and "NONE" in (out / "dev_report.md").read_text()
    reads = []
    monkeypatch.setattr(H, "simulate_cells", lambda r, cells=H.CELLS: reads.append(1) or {})
    final = H.run_confirm(root, out, reason="the one look", confirm=True)
    assert final["opened"] is False and not H.ledger_path(root).exists() and reads == [] and "NOT read" in (out / "final_report.md").read_text()


def test_the_one_look_ledger_is_written_before_the_read_and_every_refusal(tmp_path, monkeypatch):
    root = make_root(tmp_path, monkeypatch)
    cell = H.CELLS[0]
    good = {c: [T(0.4 if c == cell else -0.2, E.TEST_STARTS[k % 9] + (i + 1) * 2 * DAY, pair=H.PAIRS[i % 4]) for k in range(9) for i in range(8)] for c in H.CELLS}
    monkeypatch.setattr(H, "simulate_cells", lambda r, cells=H.CELLS: {c: good[c] for c in cells})
    out = tmp_path / "out"
    with pytest.raises(H.SealedError, match="development first"):
        H.run_confirm(root, out, reason="x", confirm=True)
    res = H.run_dev(root, out)
    assert res["passing"] == [H.cell_name(cell)]
    with pytest.raises(H.SealedError, match="--confirm"):
        H.run_confirm(root, out, reason="x", confirm=False)
    with pytest.raises(H.SealedError, match="reason"):
        H.run_confirm(root, out, reason=" ", confirm=True)
    with pytest.raises(H.SealedError, match="does not match"):
        H.run_confirm(root, out, reason="x", confirm=True, expect_plan_hash="deadbeef")
    assert not H.ledger_path(root).exists()
    seen = {}

    def spy(r, cells=H.CELLS):
        seen["ledger_first"] = H.ledger_path(root).exists()
        seen["cells"] = [H.cell_name(c) for c in cells]
        return {c: good[c] for c in cells}

    monkeypatch.setattr(H, "simulate_cells", spy)
    final = H.run_confirm(root, out, reason="the one look", confirm=True)
    assert seen["ledger_first"] is True and seen["cells"] == [H.cell_name(cell)] and final["opened"] and not final["contaminated"]  # only the cell that passed development is read
    with pytest.raises(H.SealedError, match="already read once"):
        H.run_confirm(root, out, reason="again", confirm=True)
    second = H.run_confirm(root, out, reason="again", confirm=True, second_look=True)
    assert second["contaminated"] and "SECOND LOOK" in (out / "final_report.md").read_text()
    f = root / "funding" / "BTCUSDT.csv"
    f.write_text(f.read_text() + "1,8,0.5\n")
    with pytest.raises(H.SealedError, match="data_hash differs"):
        H.run_confirm(root, out, reason="x", confirm=True, second_look=True)


def test_the_plan_hash_is_stable_and_changes_with_a_cell_a_rule_or_a_source(monkeypatch):
    h = H.plan_hash()
    assert h == H.plan_hash() and len(h) == 16
    monkeypatch.setitem(H.RULE, "dev_min_trades", 41)
    assert H.plan_hash() != h
    monkeypatch.undo()
    monkeypatch.setitem(H.DON_SETS, "s2", (56, 20))
    assert H.plan_hash() != h
    monkeypatch.undo()
    monkeypatch.setattr(H, "CELLS", H.CELLS[:-1])
    assert H.plan_hash() != h
    monkeypatch.undo()
    assert set(H.source_pins()) >= {"htf_study.py", "entry_study.py", "research/bootstrap.py"}


def test_the_average_cost_in_r_includes_funding_paid():
    t = T(0.3, E.TEST_STARTS[0] + DAY)
    t.funding_r = -0.1
    t.net_r = 0.3 - 0.1  # gross 0.5, fees and slippage 0.1, funding 0.1 paid
    t.gross_r = 0.5
    e = H.evaluate([t], H.DEV_WINDOWS, 1, 1)
    assert e["avg_cost_r"] == pytest.approx(0.3)  # gross 0.5 - net 0.2: fees and slippage 0.2 plus funding 0.1 less the 0.0 rounding; checked below
    assert e["avg_gross_r"] == 0.5
