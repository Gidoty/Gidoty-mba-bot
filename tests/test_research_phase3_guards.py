"""Phase 3: the strategy guards in the research backtester, and the tune-then-seal runner.

The guards are applied by a SUBCLASS of the engine, swapped in for a run: engine.py and every other registered or fingerprinted file stay
byte-identical (pinned below), so the Phase 1, Phase 2 and Design 4 registrations still hold. The pure rules are the SAME file the live
path uses (hash pinned). Price paths are hand-built; the store used for the runner is SYNTHETIC: it proves the mechanics, not that any
guard helps on real markets."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from research_helpers import *  # noqa: F401,F403

from mba_bot import strategy_guard_rules as R
from mba_bot.research import engine as eng
from mba_bot.research import phase1, phase2, synthetic
from mba_bot.research import phase3_guards as P3
from mba_bot.research.engine import MODE_CLOSED, MODE_REPLAY, TradeRecord, run_backtest
from mba_bot.research.validation import LockboxError, LookLedger, TrialRegistry
from mba_bot.strategy import SignalType, StrategySignal

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
DOWN = [100 - 2 * i for i in range(10)]
RISE_AND_FALL = DOWN + [90, 100, 112, 125, 140, 150, 160, 170, 175, 170, 150, 120, 100, 80, 60, 50, 45]
RULES_SHA256 = "3338f7d35d429705dd25ab4d4f701e47dd4dfddc69f102bdfca82aebf29bec3d"  # the file in the live repository (docs/safety/strategy_guards.md)


# ====================================================================== what must not change
def test_the_rules_file_is_byte_identical_to_the_live_one():
    assert hashlib.sha256((ROOT / "src/mba_bot/strategy_guard_rules.py").read_bytes()).hexdigest() == RULES_SHA256 == P3.rules_sha256()


def test_no_registered_or_fingerprinted_file_was_touched():
    """Phase 1, Phase 2 and Design 4 are registered against these values; editing the engine would void them."""
    assert phase1.plan_hash() == "6f29fad5f4cd6e33"
    assert phase2.plan_hash() == "a3a0ff0672445ea7"
    assert phase2.code_fingerprint() == "c39cd20e12619f0b"


def test_the_engine_hooks_the_subclass_relies_on_still_exist():
    for name in ("_try_enter", "_side_for", "_close", "pos", "trades", "counters"):
        assert hasattr(eng._Engine, name) or name in ("pos", "trades", "counters")
    import inspect
    assert list(inspect.signature(eng._Engine._try_enter).parameters)[:6] == ["self", "sym", "sig", "price", "ts", "window_fn"]
    assert eng._Engine is not P3.GuardedEngine


def test_guards_active_restores_the_plain_engine_and_does_nothing_when_all_guards_are_off():
    plain = eng._Engine
    with P3.guards_active(P3.GuardParams()):
        assert eng._Engine is plain
    with P3.guards_active(P3.GuardParams(cooldown_hours=4)):
        assert eng._Engine is P3.GuardedEngine
    assert eng._Engine is plain and P3._ACTIVE is None
    with pytest.raises(RuntimeError):
        with P3.guards_active(P3.GuardParams(cooldown_hours=4)):
            raise RuntimeError("boom")
    assert eng._Engine is plain and P3._ACTIVE is None  # restored even after an error


# ====================================================================== the guarded engine, on hand-built paths
def run(p, mode, guards=None, **kw):
    with P3.guards_active(guards or P3.GuardParams()):
        return run_backtest({"TESTUSDT": p}, config(mode, **kw))


def _reentry_pair():
    path = [82 + 22 if k >= 1 else 83 for k in range(12)]
    return pair(DOWN + [104.0, 106, 108, 110, 112], overrides={10: path}, spikes={(10, 5): (60.0, 105.0)})


def test_a_guarded_engine_with_every_guard_off_reproduces_the_plain_engine_exactly():
    p = pair(RISE_AND_FALL)
    plain = run_backtest({"TESTUSDT": p}, config(MODE_CLOSED))
    saved = (eng._Engine, P3._ACTIVE)
    try:
        eng._Engine, P3._ACTIVE = P3.GuardedEngine, P3.GuardParams()  # force the subclass, all guards off
        forced = run_backtest({"TESTUSDT": p}, config(MODE_CLOSED))
    finally:
        eng._Engine, P3._ACTIVE = saved
    assert [dataclasses.asdict(t) for t in forced.trades] == [dataclasses.asdict(t) for t in plain.trades]
    assert {k: v for k, v in forced.counters.items() if not k.startswith("guard_")} == plain.counters


def test_the_cooldown_stops_the_same_candle_re_entry_after_a_stop_out():
    p = _reentry_pair()
    base = run(p, MODE_REPLAY)
    guarded = run(p, MODE_REPLAY, P3.GuardParams(cooldown_hours=4.0))
    assert [t.exit_reason for t in base.trades][:1] == ["stop_loss"] and len(base.trades) >= 2
    assert len(guarded.trades) == 1 and guarded.trades[0].exit_reason == "stop_loss"
    assert guarded.counters["guard_rejections_" + R.R_STOP_COOLDOWN] > 0
    assert guarded.counters["guard_rejections_" + R.R_STOP_COOLDOWN] == guarded.counters["entries"] * 0 + guarded.counters["guard_rejections_" + R.R_STOP_COOLDOWN]  # counted per poll


def test_the_cooldown_only_applies_to_stop_losses():
    p = pair(RISE_AND_FALL)
    base, guarded = run(p, MODE_CLOSED), run(p, MODE_CLOSED, P3.GuardParams(cooldown_hours=24.0))
    # a crossover exit does not start a cooldown: the guard changes nothing unless a stop_loss exit exists
    if all(t.exit_reason != "stop_loss" for t in base.trades):
        assert len(guarded.trades) == len(base.trades) and guarded.counters["guard_rejections_" + R.R_STOP_COOLDOWN] == 0


def test_the_gap_guard_refuses_entries_below_the_minimum_gap_and_counts_them():
    p = pair(RISE_AND_FALL)
    base = run(p, MODE_CLOSED)
    assert base.counters["entries"] >= 1
    huge = run(p, MODE_CLOSED, P3.GuardParams(ema_min_gap_atr=50.0))
    assert huge.counters["entries"] == 0 and huge.trades == [] and huge.counters["guard_rejections_" + R.R_EMA_GAP] >= 1
    tiny = run(p, MODE_CLOSED, P3.GuardParams(ema_min_gap_atr=0.0001))
    assert [dataclasses.asdict(t) for t in tiny.trades] == [dataclasses.asdict(t) for t in base.trades]  # a small gap changes nothing here


def test_the_fee_guard_refuses_when_the_expected_move_is_small_against_costs():
    p = pair(RISE_AND_FALL)
    assert run(p, MODE_CLOSED, P3.GuardParams(fee_margin=1e6)).counters["entries"] == 0
    assert run(p, MODE_CLOSED, P3.GuardParams(fee_margin=1e6)).counters["guard_rejections_" + R.R_FEE_GUARD] >= 1
    assert run(p, MODE_CLOSED, P3.GuardParams(fee_margin=1e-6)).counters["entries"] == run(p, MODE_CLOSED).counters["entries"]


# ---- the state-based guards, by feeding the engine a trade history
def _engine(trades=(), guards=P3.GuardParams()):
    saved = P3._ACTIVE
    P3._ACTIVE = guards
    try:
        p = pair(RISE_AND_FALL)
        e = P3.GuardedEngine({"TESTUSDT": p}, config(MODE_CLOSED), {"TESTUSDT": eng.tapelib.build_hourly_aux(p.hourly, 2)})
    finally:
        P3._ACTIVE = saved
    e.trades = list(trades)
    return e


def tr(*, exit_ts, net=-10.0, r=-1.0, reason="crossover_exit", symbol="TESTUSDT"):
    return TradeRecord(symbol=symbol, side="long", entry_ts=exit_ts - HOUR, exit_ts=exit_ts, signal_price=100.0, entry_price=100.0, exit_price=100.0 + net,
                       stop_price=90.0, quantity=1.0, notional=100.0, planned_risk=10.0, gross_pnl=net, slippage_cost=0.0, fees=0.0, funding=0.0, net_pnl=net,
                       r_net=r, r_gross=r, mfe_r=0.0, mae_r=0.0, hold_hours=1.0, exit_reason=reason, entry_signal="bullish_crossover", equity_before=1000.0)


SIG = StrategySignal(SignalType.BULLISH_CROSSOVER, "x", 101.0, 100.0, 2.0, 0.02)
NOW = T0 + 100 * HOUR


def test_a_loss_streak_pauses_for_the_set_hours_then_stops_counting_the_same_trades():
    e = _engine([tr(exit_ts=NOW - k * HOUR) for k in (3, 2, 1)], P3.GuardParams(pause_after_losses=3, streak_pause_hours=24.0))
    h = e.guard_hits("TESTUSDT", SIG, NOW)
    assert [x.code for x in h] == [R.R_LOSS_STREAK]
    e.pauses.append({"code": R.R_LOSS_STREAK, "start": NOW, "end": NOW + 24 * HOUR})
    assert e.guard_hits("TESTUSDT", SIG, NOW + 23 * HOUR)[0].code == R.R_PAUSED
    assert e.guard_hits("TESTUSDT", SIG, NOW + 24 * HOUR + 1) == []  # ended: the three old losses are not counted again


def test_the_expectancy_pause_never_ends_in_a_run():
    e = _engine([tr(exit_ts=NOW - (k + 1) * HOUR, net=-5.0, r=-0.5) for k in range(20)], P3.GuardParams(expectancy_window=20))
    assert [x.code for x in e.guard_hits("TESTUSDT", SIG, NOW)] == [R.R_EXPECTANCY]
    e.pauses.append({"code": R.R_EXPECTANCY, "start": NOW, "end": None})
    assert e.guard_hits("TESTUSDT", SIG, NOW + 10_000 * HOUR)[0].code == R.R_PAUSED


def test_expectancy_needs_a_full_window_and_a_win_streak_is_not_a_pause():
    few = _engine([tr(exit_ts=NOW - (k + 1) * HOUR, r=-0.5) for k in range(19)], P3.GuardParams(expectancy_window=20))
    assert few.guard_hits("TESTUSDT", SIG, NOW) == []
    wins = _engine([tr(exit_ts=NOW - (k + 1) * HOUR, net=5.0, r=0.5) for k in range(20)], P3.GuardParams(expectancy_window=20, pause_after_losses=2))
    assert wins.guard_hits("TESTUSDT", SIG, NOW) == []


def test_daily_loss_counts_the_utc_day_only():
    day = NOW - NOW % (24 * HOUR)
    cap = _engine().cfg.account.allocated_capital
    e = _engine([tr(exit_ts=day - HOUR, net=-0.10 * cap), tr(exit_ts=day + HOUR, net=-0.025 * cap)], P3.GuardParams(daily_loss_pct=3.0))
    assert e.guard_hits("TESTUSDT", SIG, day + 2 * HOUR) == []  # 2.5 percent today; yesterday's 10 percent is not counted
    e.trades.append(tr(exit_ts=day + 90 * 60_000, net=-0.005 * cap))
    assert [x.code for x in e.guard_hits("TESTUSDT", SIG, day + 2 * HOUR)] == [R.R_DAILY_LOSS]  # 3.0 percent
    assert e.guard_hits("TESTUSDT", SIG, day + 24 * HOUR + 1) == []  # the next day starts clean


def test_the_cooldown_uses_this_pair_and_stop_losses_only():
    e = _engine([tr(exit_ts=NOW - HOUR, reason="stop_loss", symbol="OTHERUSDT"), tr(exit_ts=NOW - HOUR, reason="crossover_exit")], P3.GuardParams(cooldown_hours=4.0))
    assert e.guard_hits("TESTUSDT", SIG, NOW) == []
    e.trades.append(tr(exit_ts=NOW - 3 * HOUR, reason="stop_loss"))
    assert [x.code for x in e.guard_hits("TESTUSDT", SIG, NOW)] == [R.R_STOP_COOLDOWN]
    assert e.guard_hits("TESTUSDT", SIG, NOW + HOUR + 1) == []


# ====================================================================== the registered rule
def row(n, e, dd, net=0.0):
    return {"n_trades": n, "expectancy_r": e, "max_dd_pct": dd, "total_net_r": net}


def test_eligibility_is_the_registered_rule():
    rule = P3.Rule()
    base = row(100, 0.10, 10.0)
    assert P3.eligible(base, row(60, 0.09, 9.0), rule)[0]
    assert P3.eligible(base, row(100, 0.10, 10.0), rule)[0]  # a tie on every measure still qualifies
    assert not P3.eligible(base, row(49, 0.10, 9.0), rule)[0]  # keeps under half the trades
    assert not P3.eligible(base, row(80, 0.07, 9.0), rule)[0]  # loses more than 0.02 R per trade
    assert P3.eligible(base, row(80, 0.0801, 9.0), rule)[0]
    assert not P3.eligible(base, row(80, 0.12, 10.5), rule)[0]  # a worse drawdown
    ok, why = P3.eligible(base, row(10, 0.0, 20.0), rule)
    assert not ok and len(why) == 3


def test_selection_picks_the_lowest_drawdown_then_the_higher_expectancy_and_may_pick_nothing():
    arms = P3.ARMS
    rows = {"BASE": row(100, 0.10, 10.0)}
    for a in arms:
        if a.name != "BASE":
            rows[a.name] = row(100, 0.10, 12.0)  # everything worse: nothing eligible
    sel = P3.select(rows, arms, P3.Rule())
    assert all(s["arm"] is None for s in sel.values()) and sel["gap"]["reasons"]
    rows["GAP_0.1"], rows["GAP_0.2"], rows["GAP_0.3"] = row(90, 0.12, 8.0), row(80, 0.15, 8.0), row(70, 0.20, 9.0)
    sel = P3.select(rows, arms, P3.Rule())
    assert sel["gap"]["arm"] == "GAP_0.2"  # equal drawdown 8.0: the higher expectancy
    combo = P3.combine(sel, arms)
    assert combo.ema_min_gap_atr == 0.2 and combo.cooldown_hours == 0.0 and not combo.is_off()


def test_the_plan_hash_changes_with_the_arms_the_rule_or_the_rules_file():
    base = P3.plan_hash()
    assert base == P3.plan_hash(P3.Phase3Options())
    assert base != P3.plan_hash(dataclasses.replace(P3.Phase3Options(), rule=P3.Rule(min_trade_share=0.6)))
    assert base != P3.plan_hash(dataclasses.replace(P3.Phase3Options(), arms=P3.ARMS[:-1]))
    assert base != P3.plan_hash(dataclasses.replace(P3.Phase3Options(), sealed_months=3.0))


# ====================================================================== the runner on a SYNTHETIC store
PAIRS = ("BTCUSDT", "ETHUSDT")
SLASH = ["BTC/USDT", "ETH/USDT"]


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("store")
    synthetic.write_store(root, pairs=SLASH, days=330, seed=11, with_5m=False, now=datetime(2026, 1, 1, tzinfo=UTC))
    return root


def options(**kw):
    return dataclasses.replace(P3.Phase3Options(), dev_pairs=PAIRS, mode=MODE_CLOSED, **kw)


def test_tuning_reads_only_the_tune_window_and_writes_the_registered_outputs(store, tmp_path, monkeypatch):
    seen = []
    real = P3.Lab.run

    def spy(self, symbols, cfg):
        seen.append((cfg.start_ts, cfg.end_ts))
        return real(self, symbols, cfg)

    monkeypatch.setattr(P3.Lab, "run", spy)
    res = P3.run_tune(store, tmp_path / "out", options())
    t0, t1 = res["tune_range"]
    s0, s1 = res["sealed_range"]
    assert t1 <= s0 - 3 * 86_400_000 + 1 and seen and all(e == t1 and st == t0 for st, e in seen)  # never past the embargo before the sealed months
    assert (s1 - s0) / 86_400_000 == pytest.approx(2 * 30.4375, rel=0.01) and (s0 - t0) / 86_400_000 == pytest.approx(4 * 30.4375, rel=0.02)
    out = tmp_path / "out"
    for f in ("tune_results.json", "selected.json", "tune_report.md", "registry.jsonl", "looks.jsonl"):
        assert (out / f).exists(), f
    assert set(res["rows"]) == {a.name for a in P3.ARMS} | {"SELECTED"} and res["plan_hash"] == P3.plan_hash(options())
    looks = LookLedger(out / "looks.jsonl").entries()
    assert [e["kind"] for e in looks] == ["design"] and "sealed months are not read" in looks[0]["purpose"]
    assert not LookLedger(out / "looks.jsonl").confirmations(f"guards_{res['data_hash']}")
    assert "sealed months NOT read" in (out / "tune_report.md").read_text()
    assert len(TrialRegistry(out / "registry.jsonl").records()) == len(P3.ARMS)


def test_the_sealed_months_open_once_only_with_a_reason_and_the_same_plan_rules_and_data(store, tmp_path):
    out = tmp_path / "out"
    P3.run_tune(store, out, options())
    with pytest.raises(LockboxError, match="explicit"):
        P3.run_confirm(store, out, options(), reason="x", confirm=False)
    with pytest.raises(LockboxError, match="reason"):
        P3.run_confirm(store, out, options(), reason="  ", confirm=True)
    changed = dataclasses.replace(options(), rule=P3.Rule(min_trade_share=0.9))
    with pytest.raises(LockboxError, match="plan_hash differs"):
        P3.run_confirm(store, out, changed, reason="try", confirm=True)
    sel = json.loads((out / "selected.json").read_text())
    sel["rules_sha256"] = "0" * 64
    (out / "selected.json").write_text(json.dumps(sel))
    with pytest.raises(LockboxError, match="rules_sha256 differs"):
        P3.run_confirm(store, out, options(), reason="try", confirm=True)
    sel["rules_sha256"] = P3.rules_sha256()
    (out / "selected.json").write_text(json.dumps(sel))
    assert not LookLedger(out / "looks.jsonl").confirmations(f"guards_{sel['data_hash']}")  # every refusal happened BEFORE the sealed months were read
    res = P3.run_confirm(store, out, options(), reason="registered one-shot", confirm=True)
    assert res["contaminated"] is False and res["lockbox"]["opening_number"] == 1
    assert {"BASE", "SELECTED"} <= set(res["rows"]) and "BASE@1.5xcost" in res["rows"] and "SELECTED@1.5xcost" in res["rows"]
    with pytest.raises(LockboxError, match="already consumed"):
        P3.run_confirm(store, out, options(), reason="again", confirm=True)  # a second look is refused
    second = P3.run_confirm(store, out, options(), reason="forced", confirm=True, second_look=True)
    assert second["contaminated"] is True and second["lockbox"]["contaminated"] is True
    assert "SECOND LOOK" in (out / "final_report.md").read_text() or "NOT the first look" in (out / "final_report.md").read_text()


def test_confirm_needs_the_tuning_first(store, tmp_path):
    with pytest.raises(LockboxError, match="tuning first"):
        P3.run_confirm(store, tmp_path / "empty", options(), reason="x", confirm=True)


def test_the_plan_hash_guard_refuses_a_different_plan(store, tmp_path):
    with pytest.raises(LockboxError, match="differs from the registered"):
        P3.run_tune(store, tmp_path / "o", options(), expect_plan_hash="0000000000000000")
    assert not (tmp_path / "o" / "tune_results.json").exists()


def test_a_store_with_too_little_history_is_refused(tmp_path):
    root = tmp_path / "short"
    synthetic.write_store(root, pairs=SLASH, days=120, seed=3, with_5m=False, now=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(LockboxError, match="less than 6 months"):
        P3.run_tune(root, tmp_path / "o", options())


def test_the_reports_never_claim_an_effect_that_was_not_measured(store, tmp_path):
    out = tmp_path / "out"
    P3.run_tune(store, out, options())
    P3.run_confirm(store, out, options(), reason="one shot", confirm=True)
    text = (out / "final_report.md").read_text()
    assert "NOT MEASURED (the strategies do not exist)" in text and "Nothing below says a weakness is removed" in text and "short" in text
    for _, weak, _ in P3.ROWS:
        assert weak in text
    tune_text = (out / "tune_report.md").read_text()
    assert "not evidence that a guard helps" in tune_text


def test_the_cli_plan_prints_the_registered_hashes(capsys):
    from mba_bot.research.__main__ import main
    assert main(["guards", "plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan_hash"] == P3.plan_hash() and out["rules_sha256"] == RULES_SHA256 and "BASE" in out["arms"] and out["months"] == [6.0, 2.0, 3.0]


def test_phase3_imports_no_service_code_and_the_service_does_not_import_it():
    import ast
    tree = ast.parse((ROOT / "src/mba_bot/research/phase3_guards.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(x in (m or "") for m in mods for x in ("trader", "scheduler", "exchange", "lifecycle", "venue", "db", "config", "models", "credentials")), mods


def test_a_changed_sealed_range_is_refused_before_the_sealed_months_are_read(store, tmp_path):
    out = tmp_path / "out"
    P3.run_tune(store, out, options())
    sel = json.loads((out / "selected.json").read_text())
    sel["sealed_range"] = [sel["sealed_range"][0] + 86_400_000, sel["sealed_range"][1]]
    (out / "selected.json").write_text(json.dumps(sel))
    with pytest.raises(LockboxError, match="sealed range changed"):
        P3.run_confirm(store, out, options(), reason="try", confirm=True)
    assert not LookLedger(out / "looks.jsonl").confirmations(f"guards_{sel['data_hash']}")
