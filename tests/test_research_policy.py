"""Phase 1 research policies in the backtester: confirmation, direction, volatility, exits, Auto scoring, signal tapes.

Hand-built paths use tiny indicator periods (tests/research_helpers.py). Policies change nothing unless asked; the
Phase 0 regression test pins that."""
from __future__ import annotations

import dataclasses
import random

import pytest
from research_helpers import *  # noqa: F401,F403

from mba_bot.pair_selection import PairCandidate, rank_candidates
from mba_bot.research import reporting, synthetic, tape
from mba_bot.research.engine import MODE_CLOSED, MODE_REPLAY, BacktestError, Policy, _Engine, production_score, run_backtest
from mba_bot.strategy import generate_signal

DOWN = [100 - 2 * i for i in range(10)]
RISE_AND_FALL = DOWN + [90, 100, 112, 125, 140, 150, 160, 170, 175, 170, 150, 120, 100, 80, 60, 50, 45]


def run(pairs, mode, policy=None, **kw):
    cfg = config(mode, **kw)
    if policy is not None:
        cfg = dataclasses.replace(cfg, policy=policy)
    return run_backtest(pairs, cfg)


def _spike_pair():
    path = [82 + (22 if k in (1, 2, 3, 4) else (10 if k in (5, 6) else 0)) for k in range(12)]
    path[-1] = 82.0
    return pair(DOWN + [82.0, 81, 80, 79, 78, 77, 76], overrides={10: path})


def _reentry_pair():
    path = [82 + 22 if k >= 1 else 83 for k in range(12)]
    return pair(DOWN + [104.0, 106, 108, 110, 112], overrides={10: path}, spikes={(10, 5): (60.0, 105.0)})


# ------------------------------------------------------------------ tapes


def test_a_tape_holds_exactly_what_the_production_function_returned():
    p = pair(RISE_AND_FALL)
    sp = dict(ema_fast_period=2, ema_slow_period=3, atr_period=2, atr_flat_threshold=0.75, atr_rolling_window=3)
    timeline = [c.timestamp for c in p.exec_bars]
    t = tape.build_replay_tape("TESTUSDT", p.hourly, timeline, p.exec_bars, sp, limit=12, warm=8)
    assert len(t) == len(timeline)
    checked = 0
    for k in range(0, len(timeline), 7):
        hs = timeline[k] - timeline[k] % HOUR
        idx = {c.timestamp: i for i, c in enumerate(p.hourly)}[hs]
        if idx < 8:
            assert t.code(k) == tape.CODE_NA
            continue
        chunk = [c for c in p.exec_bars[: k + 1] if c.timestamp >= hs]
        forming = dataclasses.replace(p.hourly[idx], open=chunk[0].open, high=max(c.high for c in chunk), low=min(c.low for c in chunk), close=chunk[-1].close)
        direct = generate_signal(p.hourly[max(0, idx - 11) : idx] + [forming], **sp)
        got = t.signal(k)
        assert got.signal == direct.signal and got.atr == pytest.approx(direct.atr) and got.fast_ema == pytest.approx(direct.fast_ema)
        checked += 1
    assert checked > 10


def test_supplying_prebuilt_tapes_changes_nothing_but_speed():
    p = pair(RISE_AND_FALL)
    sp = dict(ema_fast_period=2, ema_slow_period=3, atr_period=2, atr_flat_threshold=0.75, atr_rolling_window=3)
    timeline = [c.timestamp for c in p.exec_bars]
    tapes = {"TESTUSDT": tape.build_replay_tape("TESTUSDT", p.hourly, timeline, p.exec_bars, sp, limit=12, warm=8)}
    a = run({"TESTUSDT": p}, MODE_REPLAY)
    b = run_backtest({"TESTUSDT": p}, config(MODE_REPLAY), tapes=tapes)
    assert [(t.entry_ts, t.exit_ts, t.net_pnl) for t in a.trades] == [(t.entry_ts, t.exit_ts, t.net_pnl) for t in b.trades]
    with pytest.raises(BacktestError):
        short = {"TESTUSDT": tape.build_replay_tape("TESTUSDT", p.hourly, timeline[:100], p.exec_bars, sp, limit=12, warm=8)}
        run_backtest({"TESTUSDT": p}, config(MODE_REPLAY), tapes=short)


def test_a_windowed_run_sees_the_same_forming_candle_a_full_run_would():
    p = pair(RISE_AND_FALL)
    full = run({"TESTUSDT": p}, MODE_REPLAY)
    t = full.trades[0]
    mid = dataclasses.replace(config(MODE_REPLAY), start_ts=t.entry_ts - 3 * HOUR, end_ts=t.exit_ts + 2 * HOUR)
    windowed = run_backtest({"TESTUSDT": p}, mid)
    assert (windowed.trades[0].entry_ts, windowed.trades[0].exit_ts) == (t.entry_ts, t.exit_ts)


def test_the_production_auto_score_orders_candidates_exactly_like_rank_candidates():
    rng = random.Random(4)
    sp = dict(ema_fast_period=2, ema_slow_period=3, atr_period=2, atr_flat_threshold=0.75, atr_rolling_window=3)
    checked = 0
    for _ in range(60):
        cands = []
        for name in ("A", "B", "C"):
            closes = [100 + sum(rng.gauss(0, 3) for _ in range(i)) / 3 for i in range(14)]
            cands.append(PairCandidate(name, [Candle(i, c, c + 1, c - 1, c, 1.0) for i, c in enumerate(closes)]))
        ranked = [r.pair for r in rank_candidates(cands, market_type="futures", **sp)]
        sigs = [(c.pair, generate_signal(c.candles, **sp)) for c in cands]
        mine = [n for n, s in sorted([x for x in sigs if x[1].signal.value != "none" and x[1].atr], key=lambda x: production_score(x[1]), reverse=True)]
        assert ranked == mine
        checked += len(ranked)
    assert checked > 20


# ------------------------------------------------------------------ H1 confirmation


def test_default_policy_is_the_production_policy():
    assert Policy().is_default() and not Policy(confirm_polls=3).is_default()


def test_requiring_the_signal_to_persist_blocks_a_flicker_and_lets_a_lasting_cross_through():
    spike = _spike_pair()
    assert run({"TESTUSDT": spike}, MODE_REPLAY).counters["entries"] == 1
    strict = run({"TESTUSDT": spike}, MODE_REPLAY, Policy(confirm_polls=12))
    assert strict.counters["entries"] == 0 and strict.counters["poll_rejections_persistence"] > 0
    mild = run({"TESTUSDT": spike}, MODE_REPLAY, Policy(confirm_polls=2))
    assert mild.counters["entries"] == 1, "a cross that lasted a few polls passes a short persistence test"
    lasting = run({"TESTUSDT": pair(RISE_AND_FALL)}, MODE_REPLAY, Policy(confirm_polls=3))
    assert lasting.counters["entries"] >= 1
    t = lasting.trades[0]
    base = run({"TESTUSDT": pair(RISE_AND_FALL)}, MODE_REPLAY).trades[0]
    assert t.entry_ts >= base.entry_ts + 2 * 300_000, "it enters later, after the cross has persisted"
    assert t.features["persist_polls"] >= 3


def test_a_margin_requirement_filters_marginal_crosses_and_records_the_margin():
    base = run({"TESTUSDT": pair(RISE_AND_FALL)}, MODE_REPLAY)
    gap = base.trades[0].features["gap_atr_at_entry"]
    assert gap is not None and gap >= 0
    blocked = run({"TESTUSDT": pair(RISE_AND_FALL)}, MODE_REPLAY, Policy(confirm_margin_atr=gap + 1.0))
    assert blocked.counters["poll_rejections_margin"] > 0
    loose = run({"TESTUSDT": pair(RISE_AND_FALL)}, MODE_REPLAY, Policy(confirm_margin_atr=gap * 0.5))
    assert loose.trades[0].entry_ts == base.trades[0].entry_ts


def test_confirmation_can_apply_to_shorts_only():
    up = [100 + 2 * i for i in range(10)]
    fall = up + [118, 100, 80, 60, 40, 30, 20, 15, 12, 10]
    p = pair(fall)
    long_only = run({"TESTUSDT": p}, MODE_REPLAY, Policy(confirm_margin_atr=99.0, confirm_sides=("long",)))
    assert any(t.side == "short" for t in long_only.trades), "shorts were not subject to the long-only rule"
    shorts = run({"TESTUSDT": p}, MODE_REPLAY, Policy(confirm_margin_atr=99.0, confirm_sides=("short",)))
    assert not any(t.side == "short" for t in shorts.trades)


def test_waiting_for_the_candle_close_enters_exactly_where_closed_mode_would():
    p = _reentry_pair()
    closed = run({"TESTUSDT": p}, MODE_CLOSED)
    waiting = run({"TESTUSDT": p}, MODE_REPLAY, Policy(wait_for_close_sides=("long", "short")))
    assert waiting.trades[0].entry_ts == closed.trades[0].entry_ts
    assert waiting.trades[0].entry_price == pytest.approx(closed.trades[0].entry_price)
    assert waiting.counters["deferred_to_candle_close"] > 0
    assert run({"TESTUSDT": _spike_pair()}, MODE_REPLAY, Policy(wait_for_close_sides=("long", "short"))).counters["entries"] == 0


def test_waiting_for_the_close_can_apply_to_one_side_only():
    p = _reentry_pair()
    longs_early = run({"TESTUSDT": p}, MODE_REPLAY, Policy(wait_for_close_sides=("short",)))
    base = run({"TESTUSDT": p}, MODE_REPLAY)
    assert longs_early.trades[0].entry_ts == base.trades[0].entry_ts, "longs keep the production timing"


# ------------------------------------------------------------------ direction


def _fall_pair():
    up = [100 + 2 * i for i in range(10)]
    return pair(up + [118, 100, 80, 60, 40, 30, 20, 15, 12, 10])


def test_each_side_can_be_switched_off_for_measurement():
    p = pair(RISE_AND_FALL)
    assert any(t.side == "long" for t in run({"TESTUSDT": p}, MODE_REPLAY).trades)
    no_long = run({"TESTUSDT": p}, MODE_REPLAY, Policy(long_allowed=False))
    assert not any(t.side == "long" for t in no_long.trades) and no_long.counters["poll_rejections_direction"] > 0
    f = _fall_pair()
    assert any(t.side == "short" for t in run({"TESTUSDT": f}, MODE_REPLAY).trades)
    assert not any(t.side == "short" for t in run({"TESTUSDT": f}, MODE_REPLAY, Policy(short_allowed=False)).trades)


@pytest.fixture(scope="module")
def synth_store(tmp_path_factory):
    root = tmp_path_factory.mktemp("pol")
    synthetic.write_store(root, pairs=["BTC/USDT", "ETH/USDT", "SOL/USDT"], days=130, seed=33)
    return root


def _real_pair(root, sym="BTC/USDT"):
    return reporting.load_pair_data(root, sym, with_5m=True)


def _prod_cfg(mode=MODE_REPLAY, **kw):
    return BacktestConfig(mode=mode, account=AccountParams(allocated_capital=2000.0), **kw)


_TAPES: dict = {}


def run_prod(pairs, cfg):
    """run_backtest with production-sized parameters, building each pair's signal tape once per test session."""
    from mba_bot.research.engine import _full_timeline

    tl = _full_timeline(pairs, True)
    key = (tuple(sorted(pairs)), len(tl), tl[0], tl[-1])
    if key not in _TAPES:
        sp = dict(ema_fast_period=20, ema_slow_period=50, atr_period=14, atr_flat_threshold=0.75, atr_rolling_window=50)
        _TAPES[key] = {s: tape.build_replay_tape(s, p.hourly, tl, p.exec_bars, sp, limit=200, warm=55) for s, p in pairs.items()}
    return run_backtest(pairs, cfg, tapes=_TAPES[key])


def test_shorts_can_be_limited_to_a_falling_market_using_only_the_trailing_return(synth_store):
    p = _real_pair(synth_store)
    base = run_prod({"BTCUSDT": p}, _prod_cfg())
    assert any(t.side == "short" and t.features.get("ret30d") is not None for t in base.trades)
    never = run_prod({"BTCUSDT": p}, _prod_cfg(policy=Policy(short_max_ret30d=-9.0)))
    late_shorts = [t for t in never.trades if t.side == "short" and t.features.get("ret30d") is not None]
    assert not late_shorts and never.counters["poll_rejections_short_trend"] > 0
    always = run_prod({"BTCUSDT": p}, _prod_cfg(policy=Policy(short_max_ret30d=9.0)))
    assert [(t.entry_ts, t.exit_ts) for t in always.trades] == [(t.entry_ts, t.exit_ts) for t in base.trades]


# ------------------------------------------------------------------ volatility


def test_the_volatility_filter_uses_only_the_pairs_own_past(synth_store):
    p = _real_pair(synth_store)
    aux = tape.build_hourly_aux(p.hourly)
    cut = len(p.hourly) // 2
    changed = [dataclasses.replace(c, high=c.high * 1.5, low=c.low * 0.5) if i > cut else c for i, c in enumerate(p.hourly)]
    aux2 = tape.build_hourly_aux(changed)
    assert aux.vol_pct[: cut + 1] == aux2.vol_pct[: cut + 1], "values before a change cannot depend on it"
    assert aux.vol_pct[500] is None and aux.vol_pct[900] is not None and 0 < aux.vol_pct[900] <= 1
    assert aux.ret30d[719] is None and aux.ret30d[720] is not None


def test_skipping_high_volatility_removes_entries_and_never_adds_any(synth_store):
    p = _real_pair(synth_store)
    base = run_prod({"BTCUSDT": p}, _prod_cfg())
    skip = run_prod({"BTCUSDT": p}, _prod_cfg(policy=Policy(vol_skip_above=0.5)))
    assert skip.counters["poll_rejections_volatility"] > 0
    assert skip.counters["entries"] <= base.counters["entries"]
    assert all(t.features["vol_pct_90d"] is None or t.features["vol_pct_90d"] <= 0.5 for t in skip.trades)


def test_half_risk_in_high_volatility_halves_the_planned_risk_only_there(synth_store):
    p = _real_pair(synth_store)
    res = run_prod({"BTCUSDT": p}, _prod_cfg(policy=Policy(vol_half_risk_above=0.5)))
    cap = 2000.0
    half = [t for t in res.trades if (t.features["vol_pct_90d"] or 0) > 0.5]
    full = [t for t in res.trades if t.features["vol_pct_90d"] is not None and t.features["vol_pct_90d"] <= 0.5]
    assert half and res.counters["half_risk_entries"] == len(half)
    assert all(t.planned_risk <= 0.5 * 0.01 * min(cap, t.equity_before) * 1.001 for t in half)
    assert any(t.planned_risk > 0.5 * 0.01 * min(cap, t.equity_before) * 1.05 for t in full) or not full


# ------------------------------------------------------------------ exits


def test_a_trailing_stop_only_ever_tightens_and_is_labelled_when_it_fires():
    p = pair(RISE_AND_FALL)
    base = run({"TESTUSDT": p}, MODE_CLOSED).trades[0]
    trail = run({"TESTUSDT": p}, MODE_CLOSED, Policy(trail_atr_mult=1.0, trail_start_r=0.5)).trades[0]
    assert trail.exit_reason == "trailing_stop" and trail.exit_ts <= base.exit_ts
    assert trail.stop_price > base.stop_price and trail.features["stop_moves"] >= 1
    assert trail.exit_price > trail.entry_price, "a long trailed up exits above where it started"


def test_a_wide_trail_leaves_the_original_exit_alone():
    p = pair(RISE_AND_FALL)
    base = run({"TESTUSDT": p}, MODE_CLOSED).trades[0]
    wide = run({"TESTUSDT": p}, MODE_CLOSED, Policy(trail_atr_mult=60.0)).trades[0]
    assert (wide.exit_ts, wide.exit_reason, wide.net_pnl) == (base.exit_ts, base.exit_reason, base.net_pnl)


def test_breakeven_moves_the_stop_to_entry_after_the_trigger_and_exits_there():
    closes = DOWN + [90, 100, 112, 125, 140, 150, 155, 160, 165, 170]
    plain = pair(closes)
    probe = run({"TESTUSDT": plain}, MODE_CLOSED).trades[0]
    assert probe.exit_reason == "end_of_data", "no crossover in this path: the plain strategy stays in"
    # a flash wick back to just below the entry price, late in the move, with the close unchanged
    wick = {(18, 4): (probe.entry_price - 3.0, 400.0)}
    p = pair(closes, spikes=wick)
    base = run({"TESTUSDT": p}, MODE_CLOSED).trades[0]
    assert base.exit_reason == "end_of_data", "the wick is far above the original stop, so production holds on"
    be = run({"TESTUSDT": p}, MODE_CLOSED, Policy(breakeven_at_r=1.0)).trades[0]
    assert be.exit_reason == "breakeven_stop" and be.exit_ts == T0 + 18 * HOUR + 5 * 300_000
    assert be.stop_price >= be.entry_price * 0.999 and abs(be.r_net) < 0.5
    off = run({"TESTUSDT": p}, MODE_CLOSED, Policy(breakeven_at_r=1000.0)).trades[0]
    assert off.exit_reason == "end_of_data", "a trigger that is never reached changes nothing"


def test_exit_policies_work_for_shorts_too():
    f = _fall_pair()
    base = run({"TESTUSDT": f}, MODE_CLOSED)
    short = next(t for t in base.trades if t.side == "short")
    trail = run({"TESTUSDT": f}, MODE_CLOSED, Policy(trail_atr_mult=1.0, trail_start_r=0.5))
    s2 = next(t for t in trail.trades if t.side == "short")
    assert s2.stop_price < short.stop_price, "a short's stop moves down toward the price"


# ------------------------------------------------------------------ Auto scoring


def _two_pairs(root):
    return {s: _real_pair(root, p) for s, p in (("BTCUSDT", "BTC/USDT"), ("ETHUSDT", "ETH/USDT"), ("SOLUSDT", "SOL/USDT"))}


def test_every_auto_score_runs_holds_one_position_and_is_reproducible(synth_store):
    pairs = _two_pairs(synth_store)
    out = {}
    for mode in ("production", "random", "calm", "momentum", "composite"):
        cfg = _prod_cfg(auto=True, policy=Policy(auto_score=mode, auto_seed=5))
        a, b = run_prod(pairs, cfg), run_prod(pairs, cfg)
        assert [(t.symbol, t.entry_ts) for t in a.trades] == [(t.symbol, t.entry_ts) for t in b.trades]
        spans = sorted((t.entry_ts, t.exit_ts) for t in a.trades)
        assert all(y[0] >= x[1] for x, y in zip(spans, spans[1:]))
        out[mode] = a
    assert out["production"].trades, "need trades to compare"
    assert {t.symbol for t in out["random"].trades} <= set(pairs)
    other = run_prod(pairs, _prod_cfg(auto=True, policy=Policy(auto_score="random", auto_seed=6)))
    assert [(t.symbol, t.entry_ts) for t in other.trades] != [(t.symbol, t.entry_ts) for t in out["random"].trades]


def test_unknown_auto_score_is_refused(synth_store):
    with pytest.raises(BacktestError):
        run_backtest(_two_pairs(synth_store), _prod_cfg(auto=True, policy=Policy(auto_score="best_guess")))


def test_pick_prefers_the_calmest_or_the_most_aligned_candidate():
    from mba_bot.strategy import SignalType, StrategySignal

    def aux(vol, ret72):
        return tape.HourlyAux([0.01] * 10, [vol] * 10, [0.0] * 10, [ret72] * 10)

    sig = lambda gap: StrategySignal(SignalType.BULLISH_CROSSOVER, "x", 100 + gap, 100.0, 1.0, 0.01)
    pairs = {s: PairData(s, [], None, [], None) for s in ("AAAUSDT", "BBBUSDT")}
    auxes = {"AAAUSDT": aux(0.9, 0.01), "BBBUSDT": aux(0.2, 0.05)}
    rows = [("AAAUSDT", sig(3.0), "long", 5, 1, 1.0), ("BBBUSDT", sig(1.0), "long", 5, 1, 1.0)]
    mk = lambda mode: _Engine(pairs, dataclasses.replace(config(MODE_CLOSED, auto=True), policy=Policy(auto_score=mode)), auxes)
    assert mk("production")._pick(rows, 0)[0] == "AAAUSDT", "the production score prefers the wider EMA gap"
    assert mk("calm")._pick(rows, 0)[0] == "BBBUSDT"
    assert mk("momentum")._pick(rows, 0)[0] == "BBBUSDT"
    assert mk("composite")._pick(rows, 0)[0] == "BBBUSDT"


def test_acting_on_the_opposite_cross_only_at_the_candle_close_ignores_an_intrabar_flip():
    # a long is open; inside hour 19 the forming candle dips far enough to show a bearish cross, then recovers by the close
    closes = DOWN + [90, 100, 112, 125, 140, 150, 155, 160, 165, 170]
    dip = [170 - (0 if k < 2 else 45 if k < 8 else 22 if k < 11 else 0) for k in range(12)]
    p = pair(closes, overrides={19: dip})
    base = run({"TESTUSDT": p}, MODE_REPLAY).trades[0]
    close_only = run({"TESTUSDT": p}, MODE_REPLAY, Policy(exit_on_close_only=True)).trades[0]
    assert base.exit_reason == "crossover_exit" and base.exit_ts < T0 + 20 * HOUR
    assert close_only.entry_ts == base.entry_ts and close_only.exit_reason == "end_of_data", "the dip had recovered by the close, so the position was kept"


# ------------------------------------------------------------------ Phase 2 additions


def test_short_risk_scale_sizes_shorts_smaller_and_leaves_longs_and_entries_alone(synth_store):
    p = _real_pair(synth_store)
    base = run_prod({"BTCUSDT": p}, _prod_cfg())
    half = run_prod({"BTCUSDT": p}, _prod_cfg(policy=Policy(short_risk_scale=0.5)))
    assert any(t.side == "short" for t in half.trades) and any(t.side == "long" for t in half.trades)
    cap = 2000.0
    for t in half.trades:
        limit = 0.01 * min(cap, t.equity_before) * (0.5 if t.side == "short" else 1.0)
        assert t.planned_risk <= limit * 1.001
    # same signals are taken (sizing only): the first trade is identical in timing and side
    assert [(t.side, t.entry_ts) for t in half.trades[:1]] == [(t.side, t.entry_ts) for t in base.trades[:1]]
    # a scale of 1.0 is exactly production
    same = run_prod({"BTCUSDT": p}, _prod_cfg(policy=Policy(short_risk_scale=1.0)))
    assert [(t.entry_ts, t.net_pnl) for t in same.trades] == [(t.entry_ts, t.net_pnl) for t in base.trades]


def test_auto_runs_log_simultaneous_signal_decisions_and_count_them(synth_store):
    pairs = _two_pairs(synth_store)
    res = run_prod(pairs, _prod_cfg(auto=True))
    c = res.counters
    assert c["decision_polls_1_candidate"] + c["decision_polls_2_candidates"] + c["decision_polls_3plus_candidates"] == c["actionable_signals"]
    multi = c["decision_polls_2_candidates"] + c["decision_polls_3plus_candidates"]
    assert len(res.decisions) == multi
    for d in res.decisions:
        assert d["chosen"] in d["candidates"] and len(d["candidates"]) >= 2 and isinstance(d["entered"], bool)
    # single-pair runs never log a decision
    assert run_prod({"BTCUSDT": pairs["BTCUSDT"]}, _prod_cfg()).decisions == []
    assert all(t.features["n_candidates"] >= 1 for t in res.trades)
