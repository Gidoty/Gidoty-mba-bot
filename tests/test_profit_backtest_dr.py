"""The sealed test of the profit rules for Donchian and RSI (src/mba_bot/profit_backtest_dr.py) and the bootstrap helper. Research only.

PROVES: with no rule the overlay reproduces the pre-registered Donchian and RSI simulations trade for trade; each overlay mechanic on hand-made candles (the lock lands at entry plus the buffer, a stop is judged first,
the higher of two stops is touched first, the strategy's own target is not pre-empted, a price through the level leaves at the close, both sides); the difference-against-baseline clustering; the bootstrap helper
(seeded, covers the mean, excludes zero for a clear effect, refuses tiny samples); the plan hash, the one-look ledger and every refusal.
DOES NOT PROVE: that any rule makes money. The numbers it produces are measurements."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from mba_bot import donchian as DN, ema_exits, profit_backtest as P, profit_backtest_dr as D
from mba_bot import rsi_mr as RM
from mba_bot import shortframe_backtest as SF
from mba_bot.research import bootstrap as B
from mba_bot.strategy import Candle

HOUR = 3_600_000


def C(i, o, h, lo, c, v=1.0, base=1_000 * HOUR):
    return Candle(base + i * HOUR, o, h, lo, c, v)


def trending(n=2500, seed=5):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for i in range(n):
        drift = 0.004 * (1 if (i // 120) % 2 == 0 else -1)
        o = price
        price *= 1 + rng.gauss(drift, 0.01)
        hi, lo = max(o, price) * (1 + abs(rng.gauss(0, 0.003))), min(o, price) * (1 - abs(rng.gauss(0, 0.003)))
        vol = 1.0 + (3.0 if rng.random() < 0.08 else 0.0) + rng.random()
        rows.append(C(i, o, hi, lo, price, vol))
    return rows


def ranging(n=3000, seed=9):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for i in range(n):
        o = price
        price += (100.0 - price) * 0.05 + rng.gauss(0, 0.9)
        rows.append(C(i, o, max(o, price) + abs(rng.gauss(0, 0.3)), min(o, price) - abs(rng.gauss(0, 0.3)), price, 1.0 + rng.random()))
    return rows


# ---------------------------------------------------------------------------------------------- the baseline reproduces the pre-registered simulations
def test_the_current_rules_reproduce_the_pre_registered_donchian_simulation():
    cs = trending()
    start, end = cs[0].timestamp, cs[-1].timestamp + HOUR
    want, _ = SF.simulate_donchian("X", cs, "1h", start, end)
    got, _ = D.simulate_donchian("X", cs, start, end, D.RS0, [])
    assert len(want) > 15 and len(got) == len(want)
    for a, b in zip(want, got):
        assert (a.entry_ts, a.exit_ts, a.reason) == (b.entry_ts, b.exit_ts, b.reason) and a.net_r == pytest.approx(b.net_r, abs=1e-9)


def test_the_current_rules_reproduce_the_pre_registered_rsi_simulation():
    cs = ranging()
    start, end = cs[0].timestamp, cs[-1].timestamp + HOUR
    want, _ = SF.simulate_rsi("X", cs, "1h", start, end)
    got, _ = D.simulate_rsi("X", cs, start, end, D.RS0, [])
    assert len(want) > 5 and len(got) == len(want)
    for a, b in zip(want, got):
        assert (a.entry_ts, a.exit_ts, a.reason) == (b.entry_ts, b.exit_ts, b.reason) and a.net_r == pytest.approx(b.net_r, abs=1e-9)


def test_the_rules_never_make_a_trade_exit_later_than_the_strategy_and_every_rule_exit_has_its_reason():
    cs = trending()
    start, end = cs[0].timestamp, cs[-1].timestamp + HOUR
    base, _ = D.simulate_donchian("X", cs, start, end, D.RS0, [])
    base_by = {t.entry_ts: t for t in base}
    for rules in D.RULE_SETS["donchian"][1:]:
        got, _ = D.simulate_donchian("X", cs, start, end, rules, [])
        assert got and any(t.reason in ("profit_lock", "break_even_stop", "trailing_stop", "take_profit") for t in got)
        for t in got:
            if t.entry_ts in base_by:
                assert t.exit_ts <= base_by[t.entry_ts].exit_ts  # an overlay can only end a trade earlier or at the same candle


# ---------------------------------------------------------------------------------------------- the overlay on hand-made candles
BUF = 100.0 * 21 / 1e4


def ov(cs, rules, *, side="long", e=None, base_price=95.0, base_reason="channel_exit", base_is_stop=False, entry=100.0, stop0=90.0, atr=1.0):
    return D.overlay(side=side, entry=entry, stop0=stop0, cs=cs, atr=[atr] * len(cs), j0=0, e=len(cs) - 1 if e is None else e, base_price=base_price, base_reason=base_reason, base_is_stop=base_is_stop, rules=rules, buffer=BUF)


def test_the_lock_lands_at_entry_plus_the_buffer_and_the_next_candle_touching_it_exits_there():
    cs = [C(0, 100, 111, 99.5, 110), C(1, 110, 110.5, 100.0, 104), C(2, 104, 104, 90, 92)]
    k, px, reason, peak = ov(cs, D.LOCK)
    assert (k, reason) == (1, "profit_lock") and px == pytest.approx(100.0 + BUF) and peak == 111.0
    k, px, reason, _ = ov(cs, D.RS0)
    assert (k, reason, px) == (2, "channel_exit", 95.0)  # without the rule the strategy's own exit stands


def test_a_price_through_the_level_at_the_close_leaves_at_the_close_and_a_level_not_below_the_peak_waits():
    cs = [C(0, 100, 111, 99.5, 100.1), C(1, 100.1, 100.2, 99.9, 100.1), C(2, 100.1, 100.2, 99.9, 100.1)]
    k, px, reason, _ = ov(cs, D.LOCK)
    assert (k, px, reason) == (0, 100.1, "profit_lock")
    tight = [C(0, 100, 100.15, 99.9, 100.1), C(1, 100.1, 100.2, 99.9, 100.1)]
    k, px, reason, _ = ov(tight, D.LOCK, stop0=99.9)  # R = 0.1, the buffer 0.21 is above the best price: waits, strategy exit stands
    assert (k, reason) == (1, "channel_exit")


def test_the_higher_of_two_stops_is_touched_first_and_the_strategys_own_target_is_not_preempted():
    cs = [C(0, 100, 111, 99.5, 110), C(1, 110, 110.5, 98.0, 99.0)]
    k, px, reason, _ = ov(cs, D.LOCK, base_price=102.0, base_reason="trailing_stop", base_is_stop=True)
    assert (k, px, reason) == (1, 102.0, "trailing_stop")  # the strategy's trail at 102 sits above the lock at 100.21: the price fell through it first
    k, px, reason, _ = ov(cs, D.LOCK, base_price=99.0, base_reason="trailing_stop", base_is_stop=True)
    assert (k, reason) == (1, "profit_lock") and px == pytest.approx(100.0 + BUF)  # here the lock is the higher stop
    tgt = [C(0, 100, 111, 99.5, 110), C(1, 110, 120, 109.0, 118)]
    k, px, reason, _ = ov(tgt, D.LOCK, base_price=118.0, base_reason="profit_target")
    assert (k, px, reason) == (1, 118.0, "profit_target")  # the candle never touched the lock: the target fills


def test_the_adapted_rules_take_profit_break_even_and_trail():
    tp = [C(0, 100, 100.4, 99.8, 100.2), C(1, 100.2, 131, 100.1, 130)]
    k, px, reason, _ = ov(tp, D.RS84X_DON, atr=2.0)
    assert (k, reason) == (1, "take_profit") and px == pytest.approx(130.0 + BUF)  # entry + 3 x R(10) + the buffer
    none, _, reason2, _ = ov(tp, D.RS84X_RSI, atr=2.0, base_price=130.0, base_reason="profit_target")
    assert reason2 == "profit_target"  # RSI keeps its own target
    be = [C(0, 100, 111, 99.5, 110), C(1, 110, 110.5, 99.0, 100.0)]
    k, px, reason, _ = ov(be, D.RS84X_RSI, atr=2.0)
    assert (k, reason) == (1, "break_even_stop") and px == pytest.approx(100.0)


def test_a_short_mirrors_the_lock():
    cs = [C(0, 100, 100.5, 89, 90), C(1, 90, 99.9, 89.5, 96), C(2, 96, 110, 95, 109)]
    k, px, reason, peak = ov(cs, D.LOCK, side="short", stop0=110.0, base_price=105.0)
    assert (k, reason) == (1, "profit_lock") and px == pytest.approx(100.0 - BUF) and peak == 89


# ---------------------------------------------------------------------------------------------- the statistics
def T(pair, ts, net):
    return P.SimTrade(pair, "long", ts, ts + HOUR, 100.0, 100.0, 10.0, 90.0, 100.0, [P.Leg(1.0, 100.0, ts, "x")], "x", net, net, 0.0, 0.0, False, None, HOUR)


def test_the_difference_is_taken_per_entry_and_a_missing_trade_counts_as_zero():
    t0 = 1_700_000_000_000
    base = [T("B", t0 + i * HOUR, 1.0) for i in range(40)]
    rule = [T("B", t0 + i * HOUR, 1.5) for i in range(40)] + [T("B", t0 + 99 * HOUR, 0.5)]
    d = D.delta_stats(rule, base)
    assert d["entries"] == 41 and d["total"] == pytest.approx(40 * 0.5 + 0.5) and d["distinguishable"] is False  # one month of trades: the block interval cannot exclude zero
    d2 = D.delta_stats(base[:10], base)
    assert d2["total"] == pytest.approx(-30.0)


def test_the_bootstrap_is_seeded_covers_the_mean_excludes_zero_for_a_clear_effect_and_refuses_tiny_samples():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0] * 20
    a, b = B.mean_ci(vals, seed=3), B.mean_ci(vals, seed=3)
    assert a == b and a[1] <= a[0] <= a[2] and a[0] == pytest.approx(3.0)
    assert B.excludes_zero(a) and not B.excludes_zero(B.mean_ci([-1.0, 1.0, -1.0, 1.0] * 5, seed=3))
    assert B.mean_ci([1.0]) is None and B.block_sum_ci({"m": 1.0}) is None and not B.excludes_zero(None)
    blocks = {f"2025-{m:02d}": 2.0 for m in range(1, 13)}
    c = B.block_sum_ci(blocks, seed=3)
    assert c[0] == 24.0 and c[1] <= 24.0 <= c[2]
    wide = B.block_sum_ci({str(i): (10.0 if i % 2 else -10.0) for i in range(12)}, seed=3)
    assert wide[1] < 0 < wide[2]  # blocks that disagree give an interval that straddles zero


# ---------------------------------------------------------------------------------------------- the plan, the ledger and every refusal
def write_store(root: Path, n=2600):
    from mba_bot.research.data import KLINE_FIELDS

    start = 1_700_000_000_000
    for k, sym in enumerate(P.PAIRS):
        cs = trending(n, seed=20 + k)
        path = root / "klines" / "1h" / f"{sym}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(",".join(KLINE_FIELDS) + "\n" + "\n".join(f"{start + i * HOUR},{c.open},{c.high},{c.low},{c.close},{c.volume},1000,5,5" for i, c in enumerate(cs)) + "\n")
        f = root / "funding" / f"{sym}.csv"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("calc_time,funding_interval_hours,last_funding_rate\n" + "\n".join(f"{start + i * 8 * HOUR},8,0.0001" for i in range(340)) + "\n")
    return start


def windows(monkeypatch, start):
    monkeypatch.setattr(D, "TUNE", (start + 400 * HOUR, start + 1800 * HOUR))
    monkeypatch.setattr(D, "CONFIRM", (start + 1810 * HOUR, start + 2500 * HOUR))


def test_the_plan_hash_is_stable_and_changes_with_a_rule_a_window_or_a_source(monkeypatch):
    h = D.plan_hash()
    assert h == D.plan_hash() and len(h) == 16
    monkeypatch.setattr(D, "BOOT", {**D.BOOT, "reps": 10})
    assert D.plan_hash() != h
    monkeypatch.undo()
    monkeypatch.setattr(D, "CONFIRM", (D.CONFIRM[0] + 1, D.CONFIRM[1]))
    assert D.plan_hash() != h
    monkeypatch.undo()
    assert set(D.source_pins()) >= {"profit_backtest_dr.py", "profit_lock.py", "research/bootstrap.py", "donchian.py", "rsi_mr.py"}


def test_tune_then_one_confirm_look_and_every_refusal(tmp_path, monkeypatch):
    root, out = tmp_path / "data", tmp_path / "out"
    start = write_store(root)
    windows(monkeypatch, start)
    monkeypatch.setattr(D, "BOOT", {**D.BOOT, "reps": 50})
    with pytest.raises(D.SealedError, match="tuning first"):
        D.run_confirm(root, out, reason="x", confirm=True)
    res = D.run_tune(root, out)
    assert (out / "tune_report.md").exists() and not D.ledger_path(root).exists() and res["rows"]["donchian"][D.RS0.name]["trades"] > 0
    with pytest.raises(D.SealedError, match="--confirm"):
        D.run_confirm(root, out, reason="x", confirm=False)
    with pytest.raises(D.SealedError, match="reason"):
        D.run_confirm(root, out, reason=" ", confirm=True)
    assert not D.ledger_path(root).exists()
    seen = {}
    real = D.run_window

    def spy(c, f, w):
        seen["ledger_first"] = D.ledger_path(root).exists()
        return real(c, f, w)

    monkeypatch.setattr(D, "run_window", spy)
    final = D.run_confirm(root, out, reason="the one look", confirm=True)
    assert seen["ledger_first"] is True and final["opened"] and not final["contaminated"] and (out / "final_report.md").exists()
    with pytest.raises(D.SealedError, match="already read once"):
        D.run_confirm(root, out, reason="again", confirm=True)
    second = D.run_confirm(root, out, reason="again", confirm=True, second_look=True)
    assert second["contaminated"] and "SECOND LOOK" in (out / "final_report.md").read_text()
    assert len(json.loads(D.ledger_path(root).read_text())[D.data_hash(root)]) == 2


def test_changed_data_or_plan_refuses_the_confirmation_and_records_nothing(tmp_path, monkeypatch):
    root, out = tmp_path / "data", tmp_path / "out"
    start = write_store(root)
    windows(monkeypatch, start)
    monkeypatch.setattr(D, "BOOT", {**D.BOOT, "reps": 50})
    D.run_tune(root, out)
    f = root / "funding" / "BTCUSDT.csv"
    f.write_text(f.read_text() + f"{start + 9999 * HOUR},8,0.5\n")
    with pytest.raises(D.SealedError, match="data_hash differs"):
        D.run_confirm(root, out, reason="x", confirm=True)
    assert not D.ledger_path(root).exists()
    sel = json.loads((out / "selected.json").read_text())
    sel["plan_hash"] = "0" * 16
    (out / "selected.json").write_text(json.dumps(sel))
    with pytest.raises(D.SealedError, match="plan_hash differs"):
        D.run_confirm(root, out, reason="x", confirm=True)


def test_a_candle_that_gaps_beyond_the_rule_stop_fills_at_its_open_the_worse_price():
    cs = [C(0, 100, 111, 99.5, 110), C(1, 95.0, 96.0, 94.0, 95.5), C(2, 95.5, 96, 94, 95)]
    k, px, reason, _ = ov(cs, D.LOCK)
    assert (k, px, reason) == (1, 95.0, "profit_lock")  # the lock stood at 100.21; the candle opened at 95
    s = [C(0, 100, 101, 89, 90), C(1, 105.0, 106.0, 104.0, 105.5), C(2, 105.5, 106, 104, 105)]
    k, px, reason, _ = ov(s, D.LOCK, side="short", stop0=110.0)
    assert (k, px, reason) == (1, 105.0, "profit_lock")
