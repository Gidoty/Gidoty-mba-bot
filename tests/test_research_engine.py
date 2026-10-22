"""Backtester V2: costs, intrabar stops, funding, sizing, the forming-candle behaviour of production.

Price paths are hand-built (tests/research_helpers.py) with deliberately tiny indicator periods, so each
scenario is small enough to reason about. The production strategy, risk and exchange-filter code run unchanged."""
from __future__ import annotations

import dataclasses

import pytest
from research_helpers import *  # noqa: F401,F403

from mba_bot.research.engine import (
    MODE_CLOSED, MODE_REPLAY, AccountParams, BacktestError, CostModel, PairData, run_backtest,
)
from mba_bot.strategy import Candle
from mba_bot.venue import FuturesVenue

DOWN = [100 - 2 * i for i in range(10)]  # 100 .. 82
RISE_AND_FALL = DOWN + [90, 100, 112, 125, 140, 150, 160, 170, 175, 170, 150, 120, 100, 80, 60, 50, 45]


def run(pairs, mode, **kw):
    return run_backtest(pairs, config(mode, **kw))


def one(mode, hour_closes=RISE_AND_FALL, **kw):
    return run({"TESTUSDT": pair(hour_closes, **{k: kw.pop(k) for k in ("funding", "overrides", "spikes", "min_notional", "wick") if k in kw})}, mode, **kw)


# ------------------------------------------------------------------ accounting


@pytest.mark.parametrize("mode", [MODE_CLOSED, MODE_REPLAY])
def test_with_every_cost_off_net_equals_gross_and_the_equity_adds_up(mode):
    r = one(mode)
    assert r.counted_trades(), "scenario must produce a trade"
    for t in r.trades:
        assert t.fees == 0 and t.slippage_cost == 0 and t.funding == 0
        assert t.net_pnl == pytest.approx(t.gross_pnl)
        sign = 1 if t.side == "long" else -1
        assert t.gross_pnl == pytest.approx((t.exit_price - t.signal_price) * t.quantity * sign)
    assert r.end_equity == pytest.approx(r.start_equity + sum(t.net_pnl for t in r.trades))


def test_fees_and_slippage_are_charged_on_both_legs_and_reconcile():
    costs = CostModel(taker_fee_bps=10.0, market_slip_bps=5.0, stop_slip_bps=0.0, per_symbol_slip_bps={}, funding=False)
    r = one(MODE_CLOSED, costs=costs)
    t = r.counted_trades()[0]
    assert t.exit_reason == "crossover_exit" and t.side == "long"
    assert t.entry_price == pytest.approx(t.signal_price * 1.0005)  # a buy fills worse
    assert t.fees == pytest.approx(0.001 * t.quantity * (t.entry_price + t.exit_price))
    ref_exit = t.exit_price / 0.9995  # a sell filled 5 bp worse than its reference
    assert t.slippage_cost == pytest.approx(t.quantity * ((t.entry_price - t.signal_price) + (ref_exit - t.exit_price)))
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.slippage_cost - t.fees)
    free = one(MODE_CLOSED).counted_trades()[0]
    assert t.net_pnl < free.net_pnl


def test_the_cost_multiplier_scales_fees_and_slippage_together():
    base = CostModel(taker_fee_bps=5.0, market_slip_bps=3.0, stop_slip_bps=8.0, per_symbol_slip_bps={}, funding=False)
    a = one(MODE_CLOSED, costs=base).counted_trades()[0]
    b = one(MODE_CLOSED, costs=dataclasses.replace(base, multiplier=2.0)).counted_trades()[0]
    assert (b.fees / b.notional) == pytest.approx(2 * (a.fees / a.notional), rel=0.02)
    assert b.slippage_cost / b.notional == pytest.approx(2 * (a.slippage_cost / a.notional), rel=0.02)


# ------------------------------------------------------------------ intrabar stops and gaps


def test_a_wick_through_the_stop_exits_at_the_stop_even_if_the_bar_closes_back_above_it():
    base = bars_5m(DOWN + [90, 100, 112, 125, 140, 150, 152, 154])
    probe = run({"TESTUSDT": pair(None, bars=base)}, MODE_CLOSED).trades[0]
    stop = probe.stop_price
    bars = list(base)
    idx = 14 * 12 + 3  # a bar well after the entry
    b = bars[idx]
    bars[idx] = Candle(b.timestamp, b.open, b.high, stop - 5.0, b.close, b.volume)  # wick through, close unchanged
    r = run({"TESTUSDT": pair(None, bars=bars)}, MODE_CLOSED)
    t = r.trades[0]
    assert t.exit_reason == "stop_loss" and t.exit_ts == bars[idx].timestamp + 300_000
    assert t.exit_price == pytest.approx(stop) and t.r_net == pytest.approx(-1.0, abs=0.12)
    assert t.mae_r >= 1.0


def test_a_gap_open_beyond_the_stop_fills_at_the_open_not_the_stop():
    base = bars_5m(DOWN + [90, 100, 112, 125, 140, 150, 152, 154])
    stop = run({"TESTUSDT": pair(None, bars=base)}, MODE_CLOSED).trades[0].stop_price
    bars = list(base)
    idx = 14 * 12 + 2
    b = bars[idx]
    gap = stop - 20.0
    bars[idx] = Candle(b.timestamp, gap, max(gap, b.close) + 1, gap - 1, gap, b.volume)
    t = run({"TESTUSDT": pair(None, bars=bars)}, MODE_CLOSED).trades[0]
    assert t.exit_reason == "stop_loss" and t.exit_price == pytest.approx(gap)
    assert t.r_net < -1.5, "a gap loses more than the planned risk"


def test_a_stop_hit_pays_the_stop_slippage():
    costs = CostModel(taker_fee_bps=0.0, market_slip_bps=0.0, stop_slip_bps=20.0, per_symbol_slip_bps={}, funding=False)
    base = bars_5m(DOWN + [90, 100, 112, 125, 140, 150, 152, 154])
    stop = run({"TESTUSDT": pair(None, bars=base)}, MODE_CLOSED).trades[0].stop_price
    bars = list(base)
    b = bars[14 * 12 + 3]
    bars[14 * 12 + 3] = Candle(b.timestamp, b.open, b.high, stop - 5.0, b.close, b.volume)
    t = run({"TESTUSDT": pair(None, bars=bars)}, MODE_CLOSED, costs=costs).trades[0]
    assert t.exit_price == pytest.approx(stop * 0.998)


# ------------------------------------------------------------------ what production does today


def _spike_pair():
    path = [82 + (22 if k in (1, 2, 3, 4) else (10 if k in (5, 6) else 0)) for k in range(12)]
    path[-1] = 82.0
    return pair(DOWN + [82.0, 81, 80, 79, 78, 77, 76], overrides={10: path})


def test_production_replay_enters_on_a_crossover_that_is_gone_by_the_candle_close_and_closed_mode_does_not():
    p = _spike_pair()
    replay = run({"TESTUSDT": p}, MODE_REPLAY)
    closed = run({"TESTUSDT": p}, MODE_CLOSED)
    assert closed.counters["entries"] == 0
    assert replay.counters["entries"] == 1
    t = replay.trades[0]
    assert t.entry_ts < T0 + 11 * HOUR, "entered during the hour whose candle never confirmed the cross"
    assert t.signal_confirmed_at_close is False
    assert t.exit_reason == "stop_loss"


def _reentry_pair():
    path = [82 + 22 if k >= 1 else 83 for k in range(12)]
    return pair(DOWN + [104.0, 106, 108, 110, 112], overrides={10: path}, spikes={(10, 5): (60.0, 105.0)})


def test_production_replay_re_enters_within_the_same_candle_after_a_stop_out_and_closed_mode_cannot():
    p = _reentry_pair()
    replay = run({"TESTUSDT": p}, MODE_REPLAY)
    closed = run({"TESTUSDT": p}, MODE_CLOSED)
    first, second = replay.trades[0], replay.trades[1]
    assert first.exit_reason == "stop_loss" and second.same_hour_reentry
    assert second.entry_ts // HOUR == first.exit_ts // HOUR or (second.entry_ts - 1) // HOUR == (first.exit_ts - 1) // HOUR
    assert replay.counters["same_hour_reentries"] == 1
    assert closed.counters["same_hour_reentries"] == 0 and closed.counters["entries"] == 1


def test_a_confirmed_crossover_is_reported_as_confirmed():
    r = one(MODE_REPLAY)
    assert r.trades[0].signal_confirmed_at_close is True


# ------------------------------------------------------------------ funding


def test_funding_is_paid_by_longs_on_a_positive_rate_received_on_a_negative_one_and_never_before_entry():
    base = one(MODE_CLOSED)
    t0 = base.counted_trades()[0]
    event_in = t0.entry_ts + 3 * HOUR
    event_before = t0.entry_ts - 2 * HOUR
    event_after = t0.exit_ts + 2 * HOUR
    costs = CostModel(taker_fee_bps=0, market_slip_bps=0, stop_slip_bps=0, per_symbol_slip_bps={}, funding=True)
    for rate, sign in ((0.001, 1), (-0.001, -1)):
        r = one(MODE_CLOSED, funding=[(event_before, 0.5), (event_in, rate), (event_after, 0.5)], costs=costs)
        t = r.counted_trades()[0]
        mark = next(c.open for c in pair(RISE_AND_FALL).exec_bars if c.timestamp <= event_in < c.timestamp + 300_000)
        assert t.funding == pytest.approx(sign * 0.001 * t.quantity * mark)
        assert t.net_pnl == pytest.approx(t.gross_pnl - t.funding)


def test_funding_can_be_switched_off():
    ev = [(T0 + 15 * HOUR, 0.01)]
    on = CostModel(taker_fee_bps=0, market_slip_bps=0, stop_slip_bps=0, per_symbol_slip_bps={}, funding=True)
    assert one(MODE_CLOSED, funding=ev, costs=on).counted_trades()[0].funding != 0
    assert one(MODE_CLOSED, funding=ev, costs=dataclasses.replace(on, funding=False)).counted_trades()[0].funding == 0


# ------------------------------------------------------------------ sizing and exchange rules


def test_risk_never_exceeds_the_configured_percentage_of_allocated_capital_even_with_a_larger_account():
    acct = AccountParams(allocated_capital=1000.0, starting_equity=10_000.0, risk_pct=1.0, max_risk_pct=2.0, max_notional_multiplier=1000.0, leverage=1000.0)
    t = one(MODE_CLOSED, account=acct).counted_trades()[0]
    assert t.planned_risk <= 10.0 + 1e-9, "sized off min(allocated, equity), never the whole account"
    assert t.planned_risk > 9.0


def test_the_notional_ceiling_binds_like_production():
    acct = AccountParams(allocated_capital=1000.0, risk_pct=1.0, max_risk_pct=2.0, max_notional_multiplier=1.0, leverage=1000.0)
    t = one(MODE_CLOSED, account=acct).counted_trades()[0]
    assert t.notional <= 1000.0 + 1e-6


def test_margin_ceiling_uses_the_account_leverage():
    acct = AccountParams(allocated_capital=1000.0, risk_pct=1.0, max_risk_pct=2.0, max_notional_multiplier=1000.0, leverage=2.0)
    t = one(MODE_CLOSED, account=acct).counted_trades()[0]
    assert t.notional / 2.0 <= 1000.0 + 1e-6


def test_exchange_minimums_block_entries_and_are_counted_not_forced():
    r = one(MODE_CLOSED, min_notional="10000000")
    assert r.trades == [] and r.counters["blocked_exchange_filters"] >= 1 and r.counters["actionable_signals"] >= 1


def test_a_small_account_cannot_reach_a_large_minimum_notional():
    acct = AccountParams(allocated_capital=50.0, risk_pct=1.0, max_risk_pct=2.0, max_notional_multiplier=3.0, leverage=5.0)
    r = one(MODE_CLOSED, account=acct, min_notional="100")
    assert r.counters["blocked_exchange_filters"] >= 1


def test_quantities_land_on_the_lot_step_and_stops_on_the_tick():
    t = one(MODE_CLOSED).counted_trades()[0]
    assert round(t.quantity / 0.001) * 0.001 == pytest.approx(t.quantity)
    assert round(t.stop_price / 0.01) * 0.01 == pytest.approx(t.stop_price)
    assert t.stop_price >= (t.signal_price - 2 * (t.signal_price - t.stop_price) / 2) - 1, "long stop below entry"
    assert t.stop_price < t.signal_price


def test_the_shim_runs_the_production_quantity_rules():
    r = rules(min_notional="20")
    shim = FuturesVenue.__new__(FuturesVenue)
    shim.rules = r
    real = FuturesVenue(None, "BTC/USDT")
    real.rules = r
    for q, px in ((0.1234567, 100.0), (0.0004, 100.0), (7.0, 3.0), (1e9, 1.0)):
        assert shim.plan_entry_quantity(q, entry_price=px, stop_price=px * 0.98, side="long") == real.plan_entry_quantity(
            q, entry_price=px, stop_price=px * 0.98, side="long"
        )


# ------------------------------------------------------------------ honesty of the replay


@pytest.mark.parametrize("mode", [MODE_CLOSED, MODE_REPLAY])
def test_no_look_ahead_a_truncated_run_agrees_with_the_full_run_on_everything_already_closed(mode):
    full_pair = pair(RISE_AND_FALL)
    cut_hours = 22
    cut_bars = full_pair.exec_bars[: cut_hours * 12]
    short_pair = PairData("TESTUSDT", [c for c in full_pair.hourly if c.timestamp < T0 + cut_hours * HOUR], cut_bars, [], full_pair.rules)
    full = run({"TESTUSDT": full_pair}, mode)
    short = run({"TESTUSDT": short_pair}, mode)
    done = [t for t in short.trades if t.exit_reason != "end_of_data"]
    assert done, "need at least one completed trade before the cut"
    key = lambda t: (t.side, t.entry_ts, t.exit_ts, round(t.net_pnl, 9), t.exit_reason)
    assert [key(t) for t in done] == [key(t) for t in full.trades if t.exit_ts <= short.span[1] and t.exit_reason != "end_of_data"][: len(done)]


def test_replay_needs_five_minute_data():
    with pytest.raises(BacktestError):
        run({"TESTUSDT": pair(RISE_AND_FALL, with_5m=False)}, MODE_REPLAY)


def test_closed_mode_runs_on_hourly_data_only_and_enters_at_the_next_open():
    r = run({"TESTUSDT": pair(RISE_AND_FALL, with_5m=False)}, MODE_CLOSED)
    assert r.counters["entries"] >= 1
    assert any("hourly-only" in n for n in r.notes)
    assert all(t.entry_ts % HOUR == 0 for t in r.trades)


def test_pair_count_rules():
    p = pair(RISE_AND_FALL)
    with pytest.raises(BacktestError):
        run_backtest({"A": p, "B": p}, config(MODE_CLOSED))
    with pytest.raises(BacktestError):
        run_backtest({"A": p}, config(MODE_CLOSED, auto=True))
    with pytest.raises(BacktestError):
        run_backtest({}, config(MODE_CLOSED))


def test_the_equity_curve_is_hourly_and_ordered():
    r = one(MODE_REPLAY)
    ts = [t for t, _ in r.equity]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    assert all(b - a == HOUR for a, b in zip(ts[1:], ts[2:]))


# ------------------------------------------------------------------ Auto Select replay


def test_auto_mode_holds_one_position_and_trades_the_pair_that_has_the_signal():
    flat = [100.0] * len(RISE_AND_FALL)
    pairs = {"AAAUSDT": pair(flat, symbol="AAAUSDT"), "TESTUSDT": pair(RISE_AND_FALL, symbol="TESTUSDT")}
    for mode in (MODE_CLOSED, MODE_REPLAY):
        r = run(pairs, mode, auto=True)
        assert r.counted_trades() and {t.symbol for t in r.counted_trades()} == {"TESTUSDT"}
        spans = sorted((t.entry_ts, t.exit_ts) for t in r.trades)
        assert all(b[0] >= a[1] for a, b in zip(spans, spans[1:])), "never two positions at once"


# ------------------------------------------------------------------ features at entry


def test_entry_features_are_recorded_for_every_trade():
    t = one(MODE_REPLAY).trades[0]
    assert t.features.get("atr") and "bar_progress_at_entry" in t.features and t.atr_pct
