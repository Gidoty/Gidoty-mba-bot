"""The rsi_mr_v1 backtest runner (src/mba_bot/rsi_backtest.py, docs/research/rsi_mr_preregistration.md).

PROVES: the simulation rules (next-open entry, slippage, fees, stop first), the metrics arithmetic, the registered selection rule, that tuning
never touches the sealed months, that the sealed months open once only and with the same plan and data, and that the reports say what is
and is not measured. The data used is SYNTHETIC (generated noise): it proves mechanics, it is NOT evidence about any strategy or market."""
from __future__ import annotations

import csv
import dataclasses
import json
import math
import random
from pathlib import Path

import pytest
from test_rsi_mr import LONG, mk

from mba_bot import rsi_backtest as B
from mba_bot import rsi_mr as R
from mba_bot.strategy import Candle

HOUR = 3_600_000


# ====================================================================== simulation, hand-built
def candles_from(closes, tail):
    """The LONG fixture, then `tail` rows (o, h, l, c) for the candles after the signal candle."""
    cs = mk(closes)
    last = cs[-1].timestamp
    for i, (o, h, l, c) in enumerate(tail, start=1):
        cs.append(Candle(last + i * HOUR, o, h, l, c, 1.0))
    return cs


def sim(cs, market="spot", p=R.RsiParams()):
    return B.simulate("BTCUSDT", cs, p, market, cs[0].timestamp, cs[-1].timestamp + HOUR)


def test_entry_is_the_next_open_with_slippage_and_the_target_exit_pays_fees_and_slippage():
    sig = R.generate_signal(mk(LONG))
    nxt_open = 94.0
    cs = candles_from(LONG, [(nxt_open, 99.0, 93.9, 98.0), (98.0, 98.1, 97.9, 98.0)])
    trades, unfinished = sim(cs, "spot")
    assert len(trades) == 1 and unfinished == 0
    t = trades[0]
    slip = 5 / 1e4
    entry = nxt_open * (1 + slip)
    stop = entry - 1.5 * sig.atr
    target = entry + 1.0 * sig.atr
    assert math.isclose(t.entry, entry) and math.isclose(t.stop, stop) and t.reason == R.EXIT_TARGET
    assert math.isclose(t.exit, target * (1 - slip))
    dist = entry - stop
    fees = 0.001 * (entry + t.exit)
    assert math.isclose(t.gross_r, (t.exit - entry) / dist) and math.isclose(t.net_r, (t.exit - entry - fees) / dist)
    assert math.isclose(t.net_pct, (t.exit - entry - fees) / entry)


def test_futures_fees_are_lower_than_spot_for_the_same_trade():
    cs = candles_from(LONG, [(94.0, 99.0, 93.9, 98.0), (98.0, 98.1, 97.9, 98.0)])
    spot, fut = sim(cs, "spot")[0][0], sim(cs, "futures")[0][0]
    assert fut.net_r > spot.net_r and math.isclose(fut.gross_r, spot.gross_r)


def test_the_stop_wins_when_one_candle_touches_both():
    sig = R.generate_signal(mk(LONG))
    cs = candles_from(LONG, [(94.0, 120.0, 80.0, 94.0), (94, 94.1, 93.9, 94)])
    t = sim(cs)[0][0]
    assert t.reason == R.EXIT_STOP and t.net_r < -1.0  # a full R lost plus costs (and slippage on the exit)


def test_a_short_needs_futures():
    short_closes = __import__("test_rsi_mr").SHORT
    cs = mk(short_closes)
    last = cs[-1].timestamp
    cs += [Candle(last + HOUR, 103.0, 103.1, 98.0, 99.0, 1.0), Candle(last + 2 * HOUR, 99.0, 99.1, 98.9, 99.0, 1.0)]
    assert sim(cs, "spot")[0] == []
    fut = sim(cs, "futures")[0]
    assert len(fut) == 1 and fut[0].side == "short" and fut[0].entry < 103.0  # sold below the open by the slippage


def test_a_trade_still_open_at_the_end_is_not_counted_and_is_reported():
    cs = candles_from(LONG, [(94.0, 94.5, 93.5, 94.0)])
    trades, unfinished = sim(cs)
    assert trades == [] and unfinished == 1


def test_no_trade_is_taken_outside_the_window():
    cs = candles_from(LONG, [(94.0, 99.0, 93.9, 98.0), (98.0, 98.1, 97.9, 98.0)])
    sig_ts = mk(LONG)[-1].timestamp
    trades, _ = B.simulate("BTCUSDT", cs, R.RsiParams(), "spot", sig_ts + HOUR, cs[-1].timestamp + HOUR)  # the window starts after the signal candle
    assert trades == []
    trades, _ = B.simulate("BTCUSDT", cs, R.RsiParams(), "spot", cs[0].timestamp, sig_ts)  # the window ends before the signal candle closes
    assert trades == []


def test_the_simulation_and_the_live_function_agree_on_the_signal():
    """The runner calls signal_from_values on precomputed series; the live path calls generate_signal. They must match on every candle."""
    from mba_bot.indicators import adx, atr, rsi
    seen = 0
    for seed in (14, 17, 19, 20, 23):  # seeds chosen because their noise produces at least one signal
        rng = random.Random(seed)
        x, closes = 100.0, []
        for _ in range(4000):
            x += -0.35 * (x - 100) + rng.gauss(0, 1.0)
            closes.append(round(x, 2))
        cs = mk(closes)
        r, a, t = rsi(closes, 14), adx([c.high for c in cs], [c.low for c in cs], closes, 14), atr([c.high for c in cs], [c.low for c in cs], closes, 14)
        crosses = [i for i in range(R.min_candles(), len(cs)) if r[i] is not None and r[i - 1] is not None and (r[i - 1] < 30 < r[i] or r[i - 1] > 70 > r[i])]
        for i in sorted(set(crosses) | set(range(R.min_candles(), len(cs), 211))):  # every RSI cross (the only place a signal can be) plus a sample
            live = R.generate_signal(cs[max(0, i - 3999): i + 1], allow_short=True)
            runner = R.signal_from_values(r[i], r[i - 1], a[i], t[i], cs[i].close, allow_short=True)
            assert (live.side, live.reason) == (runner.side, runner.reason)
            seen += live.side is not None
    assert seen >= 4


# ====================================================================== metrics
def tr(net_r, exit_ts, pct=None):
    return B.SimTrade("BTCUSDT", "long", exit_ts - HOUR, exit_ts, 100, 101, 99, "x", net_r, net_r, net_r / 100 if pct is None else pct)


def test_metrics_arithmetic():
    week = 7 * 86_400_000
    m = B.metrics([tr(1.0, 1), tr(-1.0, 2), tr(2.0, 3), tr(-1.0, 4)], 0, 4 * week)
    assert m["trades"] == 4 and math.isclose(m["trades_per_week"], 1.0) and m["win_rate"] == 0.5
    assert math.isclose(m["avg_win_r"], 1.5) and math.isclose(m["avg_loss_r"], -1.0) and math.isclose(m["expectancy_r"], 0.25) and math.isclose(m["total_net_r"], 1.0)
    # equity at 0.5 percent risk: 1.005, 1.0, 1.01, 1.005 -> the biggest fall from a peak is 1.005 -> 1.0
    assert math.isclose(m["max_dd_pct"], (1.005 - 1.005 * 0.995) / 1.005 * 100, rel_tol=1e-9)
    empty = B.metrics([], 0, week)
    assert empty["trades"] == 0 and empty["expectancy_r"] is None and empty["win_rate"] is None


def test_the_drawdown_follows_exit_order_not_input_order():
    a = [tr(-1.0, 5), tr(2.0, 1)]
    b = [tr(2.0, 1), tr(-1.0, 5)]
    assert B.metrics(a, 0, 10**9) == B.metrics(b, 0, 10**9)


# ====================================================================== selection rule
def row(n, e, dd):
    m = {"trades": n, "trades_per_week": 1.0, "expectancy_r": e, "max_dd_pct": dd}
    return {"spot": {"ALL": m}, "futures": {"ALL": m}}


def test_the_specification_stays_unless_a_variant_clearly_beats_it():
    rows = {"SPEC": row(100, 0.10, 10.0), "ADX20": row(100, 0.14, 10.0), "ADX30": row(100, 0.16, 10.9), "TARGET1.5": row(100, 0.20, 11.5), "STOP2.0": row(29, 0.9, 1.0)}
    sel = B.select(rows)
    assert sel["arm"] == "ADX30"  # +0.05 R and a drawdown inside 10 percent; TARGET1.5 has a worse drawdown, STOP2.0 too few trades, ADX20 only +0.04
    rows["ADX30"] = row(100, 0.1499, 10.0)
    assert B.select(rows)["arm"] == "SPEC"
    rows["SPEC"] = row(29, 0.10, 10.0)
    sel = B.select(rows)
    assert sel["arm"] == "SPEC" and "under 30" in sel["why"]
    rows["SPEC"] = row(100, -0.2, 10.0)
    rows["ADX20"] = row(100, -0.1, 9.0)  # still negative, but better than a negative SPEC by 0.1 R
    rows["ADX30"] = row(100, -0.3, 10.0)
    assert B.select(rows)["arm"] == "ADX20"


# ====================================================================== the runner on a SYNTHETIC store
START = 1_700_000_000_000 // HOUR * HOUR


def write_store(root: Path, *, days=330, seed=1, pairs=B.PAIRS):
    (Path(root) / "klines" / "1h").mkdir(parents=True, exist_ok=True)
    for k, sym in enumerate(pairs):
        rng = random.Random(seed * 100 + k)
        x, rows = 100.0, []
        for i in range(days * 24):
            o = x
            x += -0.15 * (x - 100) + rng.gauss(0, 0.8)
            hi, lo = max(o, x) + abs(rng.gauss(0, 0.3)), min(o, x) - abs(rng.gauss(0, 0.3))
            rows.append([START + i * HOUR, o, hi, lo, x, 1.0, 0, 0, 0])
        with (Path(root) / "klines" / "1h" / f"{sym}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ts", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume"])
            w.writerows(rows)


@pytest.fixture(scope="module")
def master_store(tmp_path_factory):
    root = tmp_path_factory.mktemp("store")
    write_store(root)
    return root


@pytest.fixture
def store(master_store, tmp_path):
    """A private copy per test: the one-look ledger now lives beside the data (audit M4), so tests that open the sealed months must not share a folder."""
    import shutil
    root = tmp_path / "data"
    shutil.copytree(master_store, root)
    return root


def test_plan_hash_changes_with_the_strategy_file_the_arms_and_the_costs(monkeypatch):
    base = B.plan_hash()
    assert base == B.plan_hash()
    monkeypatch.setattr(B, "_sha", lambda p: "deadbeef")
    assert B.plan_hash() != base  # the strategy file's bytes are part of the registration
    monkeypatch.undo()
    monkeypatch.setattr(B, "ARMS", B.ARMS[:-1])
    assert B.plan_hash() != base
    monkeypatch.undo()
    monkeypatch.setattr(B, "SLIPPAGE_BPS", 6.0)
    assert B.plan_hash() != base


def test_windows_are_four_tuned_months_an_embargo_and_two_sealed_months(store):
    data = B.load_all(store)
    w = B.windows(data)
    (t0, t1), (s0, s1) = w["tune"], w["sealed"]
    assert math.isclose((s1 - s0) / B.MONTH_MS, 2.0, rel_tol=1e-6) and math.isclose((s0 - t1) / 86_400_000, 3.0) and math.isclose((t1 - t0) / B.MONTH_MS, 4.0 - 3 / 30.4375, rel_tol=1e-6)
    assert s1 == data["BTCUSDT"][-1].timestamp + HOUR


def test_a_store_with_too_little_history_is_refused(tmp_path):
    write_store(tmp_path, days=120)
    with pytest.raises(ValueError, match="less than 6 months"):
        B.windows(B.load_all(tmp_path))


def test_tuning_never_asks_for_the_sealed_months_and_writes_its_outputs(store, tmp_path, monkeypatch):
    seen = []
    real = B.run_arm

    def spy(data, arm, s, e):
        seen.append((s, e))
        return real(data, arm, s, e)

    monkeypatch.setattr(B, "run_arm", spy)
    res = B.run_tune(store, tmp_path / "out")
    w = res["windows"]
    assert seen and all(s == w["tune"][0] and e == w["tune"][1] for s, e in seen) and w["tune"][1] < w["sealed"][0]
    for f in ("tune_results.json", "selected.json", "tune_report.md"):
        assert (tmp_path / "out" / f).exists()
    assert res["sealed_months_read"] is False and set(res["rows"]) == {a.name for a in B.ARMS}
    text = (tmp_path / "out" / "tune_report.md").read_text()
    assert "sealed months NOT read" in text and "NOT evidence that the strategy works" in text
    assert not list(store.glob("*sealed*"))


def test_the_tuning_window_cuts_trades_at_its_end(store):
    data = B.load_all(store)
    t0, t1 = B.windows(data)["tune"]
    for sym, cs in data.items():
        trades, _ = B.simulate(sym, cs, R.RsiParams(), "spot", t0, t1)
        assert all(x.exit_ts + HOUR <= t1 and x.entry_ts >= t0 for x in trades)


def test_expect_plan_hash_guards_the_run(store, tmp_path):
    with pytest.raises(ValueError, match="differs from the registered"):
        B.run_tune(store, tmp_path / "o", expect_plan_hash="0000000000000000")
    assert not (tmp_path / "o").exists()


def test_the_sealed_months_open_once_and_only_with_the_same_plan_and_data(store, tmp_path, monkeypatch):
    out = tmp_path / "out"
    B.run_tune(store, out)
    with pytest.raises(B.SealedError, match="explicit"):
        B.run_confirm(store, out, reason="x", confirm=False)
    with pytest.raises(B.SealedError, match="reason"):
        B.run_confirm(store, out, reason="  ", confirm=True)
    monkeypatch.setattr(B, "SLIPPAGE_BPS", 6.0)
    with pytest.raises(B.SealedError, match="plan_hash differs"):
        B.run_confirm(store, out, reason="try", confirm=True)
    monkeypatch.undo()
    assert not list(store.glob("*sealed*"))  # every refusal happened before the sealed months were read (no ledger beside the data)
    res = B.run_confirm(store, out, reason="registered one-shot", confirm=True)
    assert res["contaminated"] is False and res["looks"] == 1 and (out / "final_report.md").exists()
    with pytest.raises(B.SealedError, match="already read once"):
        B.run_confirm(store, out, reason="again", confirm=True)
    second = B.run_confirm(store, out, reason="forced", confirm=True, second_look=True)
    assert second["contaminated"] is True and "NOT the first look" in (out / "final_report.md").read_text()


def test_changed_data_or_a_missing_tuning_is_refused(store, tmp_path):
    with pytest.raises(B.SealedError, match="tuning first"):
        B.run_confirm(store, tmp_path / "none", reason="x", confirm=True)
    other = tmp_path / "other"
    write_store(other, seed=2)
    out = tmp_path / "out"
    B.run_tune(store, out)
    with pytest.raises(B.SealedError, match="data_hash differs"):
        B.run_confirm(other, out, reason="x", confirm=True)


def test_the_final_report_says_plainly_when_the_untouched_months_are_negative(store, tmp_path, monkeypatch):
    out = tmp_path / "out"
    B.run_tune(store, out)
    real = B.metrics

    def negative(trades, s, e):
        m = real(trades, s, e)
        if m["trades"]:
            m = {**m, "expectancy_r": -0.1}
        return m

    monkeypatch.setattr(B, "metrics", negative)
    B.run_confirm(store, out, reason="one shot", confirm=True)
    text = (out / "final_report.md").read_text()
    assert "NEGATIVE after fees and slippage" in text and "The strategy lost money there" in text
    assert "NOT MEASURED" in text and "donchian_breakout_v1 does not exist" in text  # the combined three-strategy frequency is not claimed
    for sym in B.PAIRS:
        assert sym in text
    assert "spot" in text and "futures" in text


def test_the_final_report_never_claims_a_result_it_did_not_measure(store, tmp_path):
    out = tmp_path / "out"
    B.run_tune(store, out)
    B.run_confirm(store, out, reason="one shot", confirm=True)
    text = (out / "final_report.md").read_text()
    assert "profitable" not in text.lower() or "NOT" in text
    assert "can be luck" in text or "NEGATIVE" in text


def test_the_cli_plan_prints_the_hash(capsys):
    assert B.main(["plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan_hash"] == B.plan_hash() and out["arms"][0] == "SPEC" and out["months"] == [6.0, 2.0, 3.0]
    assert B.main(["tune", "--root", "/nonexistent-dir"]) == 2


def test_the_runner_has_no_network_database_or_secret_access():
    import ast
    tree = ast.parse(Path(B.__file__).read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    banned = ("requests", "httpx", "urllib", "socket", "ccxt", "sqlalchemy", "trader", "exchange", "config", "models", "db", "crypto")
    assert not [m for m in mods if any(b == m.split(".")[-1] or b == m for b in banned)], mods
