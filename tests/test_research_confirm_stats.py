"""Phase 2 statistics: paired block bootstrap, design effect, robust summaries, Holm, cost break-even, regimes, event days."""
from __future__ import annotations

import random

from mba_bot.research import confirm_stats as cs
from mba_bot.research import phase1
from mba_bot.research.engine import TradeRecord
from mba_bot.strategy import Candle

DAY = cs.DAY_MS
HOUR = cs.HOUR_MS


def trade(r, day, symbol="BTCUSDT", side="long", reason="stop", hold_days=1):
    ts = 1_700_000_000_000 + day * DAY
    return TradeRecord(symbol=symbol, side=side, entry_ts=ts, exit_ts=ts + hold_days * DAY, signal_price=100, entry_price=100, exit_price=100,
                       stop_price=99, quantity=1, notional=100, planned_risk=1, gross_pnl=r, slippage_cost=0, fees=0, funding=0, net_pnl=r,
                       r_net=r, r_gross=r, mfe_r=0, mae_r=0, hold_hours=24, exit_reason=reason, entry_signal="x", equity_before=1000)


def test_paired_bootstrap_detects_a_real_shift_and_ignores_end_of_data():
    rng = random.Random(1)
    base = [trade(rng.gauss(0, 1), d) for d in range(0, 400, 2)]
    cand = [trade(t.r_net + 0.5, d) for t, d in zip(base, range(0, 400, 2))]
    cand.append(trade(99.0, 3, reason="end_of_data"))
    out = cs.paired_block_bootstrap(cand, base, iterations=1000)
    assert abs(out["delta"] - 0.5) < 1e-9
    assert out["low"] > 0 and out["p_positive"] > 0.99 and out["p_one_sided"] < 0.01
    assert out["n_candidate"] == len(base)


def test_paired_bootstrap_identical_arms_give_zero_difference_with_no_false_edge():
    base = [trade(random.Random(d).gauss(0, 1), d) for d in range(300)]
    out = cs.paired_block_bootstrap(list(base), base, iterations=500)
    assert out["delta"] == 0 and out["low"] == 0 and out["high"] == 0 and out["p_positive"] == 0.0


def test_paired_bootstrap_is_deterministic_and_handles_empty():
    base = [trade(float(d % 3 - 1), d) for d in range(200)]
    cand = base[::2]
    assert cs.paired_block_bootstrap(cand, base, seed=3, iterations=300) == cs.paired_block_bootstrap(cand, base, seed=3, iterations=300)
    assert cs.paired_block_bootstrap([], base) is None


def test_design_effect_exceeds_one_for_clustered_returns():
    rng = random.Random(2)
    trades = []
    for block in range(30):
        level = rng.gauss(0, 1)
        trades += [trade(level + rng.gauss(0, 0.1), block * 14 + k % 14) for k in range(14)]
    out = cs.design_effect(trades)
    assert out["design_effect"] > 3 and out["effective_n"] < out["n"] / 3
    assert cs.design_effect(trades[:10]) is None


def test_robust_summary_shows_tail_dependence():
    rs = [-1.0] * 90 + [8.0] * 10
    out = cs.robust_summary([trade(x, i) for i, x in enumerate(rs)])
    assert abs(out["mean"] - (-0.9 + 0.8)) < 1e-9
    assert out["n_ge_3r"] == 10
    assert out["mean_without_top_5pct"] < out["mean"]
    assert cs.robust_summary([]) == {"n": 0}


def test_retention_ratios():
    base = [trade(x, i) for i, x in enumerate([-1] * 80 + [4] * 20)]
    cand = [trade(x, i) for i, x in enumerate([-1] * 40 + [4] * 10)]
    out = cs.retention(base, cand)
    assert out["big_winner_ratio"] == 0.5 and out["trade_ratio"] == 0.5


def test_holm_is_monotone_capped_and_matches_hand_calculation():
    out = cs.holm_adjust({"a": 0.01, "b": 0.04, "c": 0.03})
    assert abs(out["a"] - 0.03) < 1e-12 and abs(out["c"] - 0.06) < 1e-12 and abs(out["b"] - 0.06) < 1e-12
    capped = cs.holm_adjust({"x": 0.9, "y": 0.8})
    assert capped == {"x": 1.0, "y": 1.0}


def test_break_even_multiplier_recovers_a_known_line():
    out = cs.break_even_multiplier([(0.0, 0.136), (1.0, 0.037), (2.0, -0.062), (3.0, -0.161)])
    assert abs(out["gross"] - 0.136) < 1e-9 and abs(out["cost_per_unit"] - 0.099) < 1e-9
    assert abs(out["multiplier"] - 0.136 / 0.099) < 1e-6 and out["r2"] > 0.999
    assert cs.break_even_multiplier([(1.0, 0.1)])["multiplier"] is None
    assert cs.break_even_multiplier([(1.0, 0.1), (2.0, 0.2)])["multiplier"] is None  # rising line: no break-even


def _btc(daily_returns, start=100.0):
    price, out = start, []
    for i, r in enumerate(daily_returns):
        for h in range(24):
            price *= (1 + r) ** (1 / 24)
            out.append(Candle(1_700_000_000_000 // DAY * DAY + i * DAY + h * HOUR, price, price, price, price, 1.0))
    return out


def test_regime_index_labels_bull_bear_range_and_unknown_and_is_causal():
    bars = _btc([0.004] * 150 + [-0.004] * 200 + [0.0] * 150)
    idx = cs.RegimeIndex(bars)
    assert idx.at(bars[100].timestamp) == "unknown"
    assert idx.at(bars[24 * 120].timestamp) == "bull"
    assert idx.at(bars[24 * 340].timestamp) == "bear"
    assert idx.at(bars[-1].timestamp) == "range"
    # causal: appending future bars never changes an earlier label
    longer = cs.RegimeIndex(bars + _btc([0.05] * 30, start=bars[-1].close))
    assert longer.at(bars[24 * 120].timestamp) == idx.at(bars[24 * 120].timestamp)


def test_event_days_pick_the_extreme_moves_and_overlap():
    moves = [0.001] * 99 + [0.2]
    bars = _btc(moves)
    days = cs.event_days(bars, quantile=0.99)
    last_day = bars[-1].timestamp // DAY
    assert last_day in days and len(days) <= 3
    t_in = trade(1, 0)
    t_in.entry_ts, t_in.exit_ts = last_day * DAY, last_day * DAY + HOUR
    t_out = trade(1, 0)
    t_out.entry_ts, t_out.exit_ts = (last_day - 50) * DAY, (last_day - 50) * DAY + HOUR
    assert cs.overlaps_event(t_in, days) and not cs.overlaps_event(t_out, days)


def test_ulcer_index():
    assert cs.ulcer_index([]) is None
    assert cs.ulcer_index([(0, 100.0), (1, 110.0), (2, 120.0)]) == 0
    flat_dip = cs.ulcer_index([(0, 100.0), (1, 90.0), (2, 100.0)])
    assert abs(flat_dip - (100 / 3) ** 0.5) < 1e-9


def test_matched_kept_removed_separates_filtered_trades_and_flags_new_ones():
    base = [trade(1.0, d) for d in range(40)] + [trade(-1.0, d, symbol="ETHUSDT") for d in range(40)]
    cand = [t for t in base if t.symbol == "BTCUSDT"] + [trade(0.3, 500, symbol="XRPUSDT")]
    out = cs.matched_kept_removed(base, cand)
    assert out["n_kept"] == 40 and out["n_removed"] == 40 and out["n_new"] == 1
    assert out["kept_mean"] == 1.0 and out["removed_mean"] == -1.0 and out["gap_kept_minus_removed"] == 2.0
    assert out["gap_bootstrap"]["delta"] == 2.0


def test_phase1_registered_plan_hash_is_unchanged_by_post_registration_fields():
    assert phase1.plan_hash() == "6f29fad5f4cd6e33"
