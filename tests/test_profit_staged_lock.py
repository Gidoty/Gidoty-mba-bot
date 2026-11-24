"""Part C: the staged profit lock (docs/safety/profit_staged_lock.md), one shared module for EMA, Donchian and RSI.

PROVES: the cost buffer (both fees, slippage, funding paid), each stage and the trail (activation level, the tiered distance, the time condition, the stage 1 ATR placement), every stage OFF by default and a typo never
switching one on, that a stop never loosens and a level beyond the best price is never used, that live moves go through lifecycle.move_stop (new stop first) and exits through execute_exit for EMA, Donchian and RSI trades,
and that the paper path ratchets the same levels.
DOES NOT PROVE: that any setting makes money (Part H measures that), or Binance behaviour (UNVERIFIED)."""
from __future__ import annotations

import pytest
from helpers_live import run_cycle, the_trade
from helpers_profit import HOUR, candles, live, resting_stops

from mba_bot import config, profit_protect as pp, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade

L = pp.LockRules


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def lvl(side="long", entry=100.0, initial=90.0, peak=112.0, atr=2.0, held=0.0, rules=None, buffer=0.21):
    return pp.staged_level(side=side, entry=entry, initial_stop=initial, peak=peak, atr=atr, held_candles=held, rules=rules or L(stage1_r=1.0), buffer=buffer)


# ---------------------------------------------------------------------------------------------- the cost buffer
def test_the_buffer_is_both_fees_plus_slippage_plus_funding_paid_and_never_negative():
    assert pp.cost_buffer(100.0, fee_bps_per_side=5.0, slippage_bps_round_trip=11.0) == pytest.approx(0.21)  # 2 x 5 + 11 = 21 bps of 100
    assert pp.cost_buffer(100.0, fee_bps_per_side=4.5, slippage_bps_round_trip=11.0) == pytest.approx(0.20)  # a real tier changes it
    assert pp.cost_buffer(100.0, fee_bps_per_side=5.0, slippage_bps_round_trip=11.0, funding_paid_per_unit=0.05) == pytest.approx(0.26)
    assert pp.cost_buffer(100.0, fee_bps_per_side=5.0, slippage_bps_round_trip=11.0, funding_paid_per_unit=-0.5) == pytest.approx(0.21)  # funding RECEIVED never shrinks it
    assert pp.cost_buffer(100.0, fee_bps_per_side=0.0, slippage_bps_round_trip=0.0) == 0.0 and pp.cost_buffer(-1.0, fee_bps_per_side=5.0, slippage_bps_round_trip=5.0) == 0.0


# ---------------------------------------------------------------------------------------------- stage 1
def test_stage_1_waits_for_its_activation_level_then_sits_at_the_entry_plus_the_buffer():
    assert lvl(peak=109.9) is None  # not up 1 R yet
    assert lvl(peak=110.0) == (pytest.approx(100.21), "break_even_stop")
    assert lvl(side="short", initial=110.0, peak=90.0) == (pytest.approx(99.79), "break_even_stop")  # mirrored: below the entry for a short
    # the activation level is a parameter: normal pullbacks early in a trade do not touch the stop
    for act, ok_peak, no_peak in ((1.5, 115.0, 114.9), (2.0, 120.0, 119.9)):
        r = L(stage1_r=act)
        assert lvl(peak=ok_peak, rules=r) is not None and lvl(peak=no_peak, rules=r) is None


def test_stage_1_can_sit_beyond_normal_noise_the_better_of_entry_plus_buffer_and_peak_minus_n_atr():
    r = L(stage1_r=1.0, stage1_atr=3.0)
    assert lvl(peak=112.0, atr=2.0, rules=r)[0] == pytest.approx(106.0)
    assert lvl(peak=111.0, atr=10.0, rules=r)[0] == pytest.approx(100.21)  # a wide ATR: the entry + buffer is the floor
    assert lvl(side="short", initial=110.0, peak=88.0, atr=2.0, rules=r)[0] == pytest.approx(94.0)


def test_a_level_that_is_not_below_the_best_price_is_never_used_so_a_small_r_cannot_force_an_instant_exit():
    # R = 0.1 but the buffer is 0.21: at +1 R (peak 100.1) entry + buffer (100.21) is ABOVE the best price: stage 1 waits
    assert lvl(initial=99.9, peak=100.1) is None
    assert lvl(initial=99.9, peak=100.3) == (pytest.approx(100.21), "break_even_stop")
    assert lvl(side="short", initial=100.1, peak=99.9) is None


# ---------------------------------------------------------------------------------------------- stage 2 and the trail
def test_stage_2_locks_a_set_number_of_r_and_is_off_when_its_lock_is_not_below_its_activation():
    r = L(stage2_r=2.0, stage2_lock_r=1.0)
    assert lvl(peak=119.9, rules=r) is None
    assert lvl(peak=120.0, rules=r) == (110.0, "profit_lock")
    assert lvl(side="short", initial=110.0, peak=80.0, rules=r) == (90.0, "profit_lock")
    for bad in (L(stage2_r=2.0, stage2_lock_r=2.0), L(stage2_r=2.0, stage2_lock_r=3.0), L(stage2_r=0.0, stage2_lock_r=1.0)):
        assert not bad.stage2_on and not bad.any_on
        assert lvl(peak=130.0, rules=bad) is None


def test_the_trail_distance_is_wide_at_low_profit_and_tightens_as_profit_grows_and_with_time():
    tiers = ((0.0, 3.0), (2.0, 2.0), (4.0, 1.5))
    r = L(trail_start_r=1.0, trail_atr=2.0, trail_tiers=tiers)
    assert pp.trail_multiplier(r, 1.0, 0) == 3.0 and pp.trail_multiplier(r, 1.99, 0) == 3.0
    assert pp.trail_multiplier(r, 2.0, 0) == 2.0 and pp.trail_multiplier(r, 3.9, 0) == 2.0 and pp.trail_multiplier(r, 4.0, 0) == 1.5 and pp.trail_multiplier(r, 9.0, 0) == 1.5
    assert pp.trail_multiplier(L(trail_atr=2.0), 5.0, 0) == 2.0  # no tiers: the plain distance
    t = L(trail_start_r=1.0, trail_tiers=tiers, tighten_after_candles=6, tighten_trail_atr=1.0)
    assert pp.trail_multiplier(t, 1.0, 5.9) == 3.0 and pp.trail_multiplier(t, 1.0, 6.0) == 1.0  # the time condition caps it
    assert pp.trail_multiplier(L(trail_start_r=1.0, trail_tiers=((0.0, 0.9),), tighten_after_candles=1, tighten_trail_atr=0.5), 1.0, 5) == 0.5
    assert pp.trail_multiplier(L(tighten_after_candles=0, tighten_trail_atr=1.0, trail_atr=2.0), 1.0, 99) == 2.0  # one half set is off
    assert pp.trail_multiplier(L(tighten_after_candles=5, tighten_trail_atr=0.0, trail_atr=2.0), 1.0, 99) == 2.0


def test_the_trail_starts_at_its_own_level_and_uses_the_tier_for_the_peak_profit():
    r = L(trail_start_r=1.0, trail_tiers=((0.0, 3.0), (2.0, 2.0)))
    assert lvl(peak=109.9, rules=r) is None
    assert lvl(peak=115.0, atr=2.0, rules=r) == (pytest.approx(109.0), "trailing_stop")  # +1.5 R: 3 ATR
    assert lvl(peak=125.0, atr=2.0, rules=r) == (pytest.approx(121.0), "trailing_stop")  # +2.5 R: 2 ATR
    assert lvl(peak=125.0, atr=None, rules=r) is None and lvl(peak=125.0, atr=0.0, rules=r) is None
    assert lvl(side="short", initial=110.0, peak=75.0, atr=2.0, rules=r) == (pytest.approx(79.0), "trailing_stop")


def test_the_most_favourable_earned_level_wins_and_is_always_on_the_loss_side_of_the_peak():
    r = L(stage1_r=1.0, stage2_r=2.0, stage2_lock_r=1.0, trail_start_r=2.0, trail_atr=2.0)
    got = lvl(peak=130.0, atr=2.0, rules=r)
    assert got == (126.0, "trailing_stop")  # trail 130-4 beats lock 110 and break-even 100.21
    assert lvl(peak=130.0, atr=20.0, rules=r) == (110.0, "profit_lock")  # a huge ATR: the lock is the best
    for peak in (110.0, 112.0, 120.0, 125.0, 150.0):
        for atr in (0.5, 2.0, 9.0):
            g = lvl(peak=peak, atr=atr, rules=r)
            assert g is None or g[0] < peak
            g = lvl(side="short", initial=110.0, peak=200.0 - peak, atr=atr, rules=r)
            assert g is None or g[0] > 200.0 - peak


# ---------------------------------------------------------------------------------------------- settings
def test_every_stage_is_off_by_default_and_a_typo_never_switches_one_on(monkeypatch):
    names = ("PROFIT_LOCK_STAGE1_R", "PROFIT_LOCK_STAGE1_ATR", "PROFIT_LOCK_STAGE2_R", "PROFIT_LOCK_STAGE2_LOCK_R", "PROFIT_TRAIL_START_R", "PROFIT_TRAIL_ATR", "PROFIT_TRAIL_TIERS", "PROFIT_TIGHTEN_AFTER_CANDLES", "PROFIT_TIGHTEN_TRAIL_ATR")
    for n in names:
        monkeypatch.delenv(n, raising=False)
    s = config.load_settings()
    assert not pp.lock_rules_from_settings(s).any_on
    assert (s.profit_lock_stage1_r, s.profit_lock_stage1_atr, s.profit_lock_stage2_r, s.profit_trail_start_r, s.profit_tighten_after_candles, s.profit_tighten_trail_atr, s.profit_trail_tiers) == (0, 0, 0, 0, 0, 0, ())
    for n in ("PROFIT_LOCK_STAGE1_R", "PROFIT_LOCK_STAGE2_R", "PROFIT_TRAIL_START_R", "PROFIT_LOCK_STAGE1_ATR", "PROFIT_TIGHTEN_TRAIL_ATR"):
        for bad in ("abc", "", "-1", "0", "nan", "inf", " ", "1,5"):
            monkeypatch.setenv(n, bad)
            assert not pp.lock_rules_from_settings(config.load_settings()).any_on, (n, bad)
        monkeypatch.delenv(n)
    monkeypatch.setenv("PROFIT_LOCK_STAGE1_R", "0.01")
    monkeypatch.setenv("PROFIT_LOCK_STAGE1_ATR", "99")
    s = config.load_settings()
    assert (s.profit_lock_stage1_r, s.profit_lock_stage1_atr) == (0.25, 10.0)  # clamped
    monkeypatch.setenv("PROFIT_TRAIL_ATR", "abc")
    assert config.load_settings().profit_trail_atr == 2.0  # a typo falls back to the default distance, it never switches the trail on (the start level does that)


def test_the_tiers_parse_sorted_and_any_bad_token_drops_all_of_them(monkeypatch):
    assert config.parse_trail_tiers("4:1.5, 0:3,2:2") == ((0.0, 3.0), (2.0, 2.0), (4.0, 1.5))
    for bad in ("", None, "abc", "0:3,2", "0:3,2:x", "0:3,0:2", "-1:3", "0:0.1", "0:11", "0:3;2:2", "51:2", "nan:2", "0:inf"):
        assert config.parse_trail_tiers(bad) == (), bad


# ---------------------------------------------------------------------------------------------- live, EMA (entry 90, initial stop 43, R = 47)
def test_with_every_stage_off_nothing_moves_however_far_the_price_goes(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch)
    candles(client, base, [(95, 100, 92, 99), (99, 300, 98, 290)])
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == 43.0 and len(client.orders_by_role("stop")) == 1


def test_stage_1_moves_the_stop_to_entry_plus_the_buffer_through_move_stop(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, profit_lock_stage1_r=1.0)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])  # peak 140 = +1.06 R
    run_cycle(aid, settings)
    t = the_trade(aid)
    buf = 90.0 * (2 * 5.0 + 11.0) / 10_000
    assert t.stop_loss_price == pytest.approx(round(90.0 + buf, 2), abs=0.011) and t.stop_loss_price > 90.0  # above zero net, not at the entry
    assert len(resting_stops(client)) == 1 and resting_stops(client)[0]["trigger"] == pytest.approx(t.stop_loss_price) and client.violations == [] and t.initial_stop_price == 43.0
    assert t.status == "open" and t.protection_status == "protected"


def test_stage_1_does_not_move_before_its_activation_level(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, profit_lock_stage1_r=1.5)  # 1.5 R = 160.5
    candles(client, base, [(95, 100, 92, 99), (99, 150, 98, 149)])
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == 43.0
    candles(client, base, [(95, 100, 92, 99), (99, 165, 98, 164)])
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price > 90.0


def test_stage_2_locks_one_r_and_never_loosens_a_better_stop(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, profit_lock_stage2_r=2.0, profit_lock_stage2_lock_r=1.0)
    candles(client, base, [(95, 100, 92, 99), (99, 190, 98, 189)])  # 2 R = 184
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == pytest.approx(137.0)
    with session_scope() as s:
        s.get(Trade, the_trade(aid).id).stop_loss_price = 160.0
    before = len(client.orders_by_role("stop"))
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == 160.0 and len(client.orders_by_role("stop")) == before


def test_a_pullback_through_the_stage_1_level_before_the_stop_could_move_exits_near_the_entry_through_execute_exit(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, profit_lock_stage1_r=1.0)
    # the trade went up to 140 (+1.06 R) and was back at 90.1 by the time the bot looked: the exchange stop still rests at 43, the earned level (90.19) is already above the price
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 100), (100, 100, 90, 90.1)])
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "break_even_stop" and client.position == 0.0 and len(client.orders_by_role("exit")) == 1
    assert t.exit_price == pytest.approx(90.1, abs=0.2)  # near the entry (90), not near the original stop (43)
    assert client.orders_by_role("stop")[0]["status"] == "canceled"


def test_a_stop_that_already_rests_at_the_earned_level_is_left_to_the_exchange(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, profit_lock_stage1_r=1.0)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    stops = len(client.orders_by_role("stop"))
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 100), (100, 100, 90, 90.1)])  # the price is now below the resting stop: the exchange's stop owns that exit
    run_cycle(aid, settings)
    assert the_trade(aid).status == "open" and len(client.orders_by_role("stop")) == stops and len(client.orders_by_role("exit")) == 0


_TRAIL = {}


@pytest.mark.parametrize("name,extra", [("wide", {}), ("tight", {"profit_tighten_after_candles": 2, "profit_tighten_trail_atr": 0.5})])
def test_the_trail_with_and_without_the_time_condition(db, monkeypatch, name, extra):
    client, settings, aid, base = live(monkeypatch, profit_trail_start_r=1.0, profit_trail_atr=4.0, **extra)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    _TRAIL[name] = the_trade(aid).stop_loss_price


def test_the_time_condition_tightens_the_trail_after_the_set_number_of_candles():
    assert _TRAIL["tight"] > _TRAIL["wide"] >= 43.0  # the trade was opened 8 candles ago: 4 ATR wide vs 0.5 ATR tight
    assert _TRAIL["tight"] < 140.0


# ---------------------------------------------------------------------------------------------- live, Donchian and RSI trades
def test_a_live_donchian_trade_gets_the_staged_lock_too(db, monkeypatch):
    from test_live_donchian import _later, live_account, raw, rising, setup

    settings, client = setup(monkeypatch, profit_lock_stage1_r=1.0)
    aid = live_account(settings)
    run_cycle(aid, settings)
    first = the_trade(aid)
    monkeypatch.setattr(trader.donchian_trail, "trailing_level", lambda **k: None)  # isolate the lock from the strategy's own trail
    cs = rising(4)
    client._ohlcv, client.price = raw(cs), cs[-1].close + 0.5
    _later(aid, 5.5)
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.stop_loss_price == pytest.approx(first.entry_price * (1 + 21 / 10_000), abs=0.011) and t.stop_loss_price < client.price and len(resting_stops(client)) == 1 and client.violations == [] and t.initial_stop_price == first.initial_stop_price


def test_a_live_rsi_trade_gets_the_staged_lock_too(db, monkeypatch):
    from test_live_rsi import _opened_hours_ago, live_account, raw, setup

    from mba_bot import rsi_mr
    from test_rsi_mr import LONG

    settings, client = setup(monkeypatch, market="futures", profit_lock_stage1_r=0.25)
    aid = live_account(settings, market="futures")
    run_cycle(aid, settings)
    first = the_trade(aid)
    assert first.strategy_id == "rsi_mr_v1"
    monkeypatch.setattr(rsi_mr, "exit_check", lambda **k: None)  # isolate the lock from the RSI exits
    recovered = list(LONG) + [97.5, 99.0, 100.5, 101.5, 102.0, 102.5]
    client._ohlcv, client.price = raw(recovered), float(recovered[-1])
    _opened_hours_ago(aid, 6.5)
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and t.peak_price >= 102.0 and t.stop_loss_price >= first.entry_price and t.stop_loss_price > first.stop_loss_price and client.violations == []


# ---------------------------------------------------------------------------------------------- paper
def test_the_paper_ratchet_sets_the_stop_and_a_price_through_the_level_exits_at_the_price():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from mba_bot import trader

    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    cs = [SimpleNamespace(timestamp=now_ms - (30 - i) * HOUR, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0) for i in range(30)]
    s = config.load_settings()
    s = __import__("dataclasses").replace(s, profit_lock_stage1_r=1.0)
    t = SimpleNamespace(side="long", entry_price=100.0, initial_stop_price=90.0, stop_loss_price=90.0, peak_price=112.0, opened_at=now - timedelta(hours=5))
    trader._paper_staged_ratchet(t, cs, 111.0, s, tf_ms=HOUR, atr_period=14, now_ms=now_ms)
    assert t.stop_loss_price == pytest.approx(100.0 + 100.0 * 21 / 10_000)
    t.stop_loss_price = 90.0
    trader._paper_staged_ratchet(t, cs, 100.05, s, tf_ms=HOUR, atr_period=14, now_ms=now_ms)  # the price is already below the earned level: the stop is set AT the price
    assert t.stop_loss_price == pytest.approx(100.05)
    t.stop_loss_price = 105.0
    trader._paper_staged_ratchet(t, cs, 111.0, s, tf_ms=HOUR, atr_period=14, now_ms=now_ms)
    assert t.stop_loss_price == 105.0  # never loosened
    short = SimpleNamespace(side="short", entry_price=100.0, initial_stop_price=110.0, stop_loss_price=110.0, peak_price=88.0, opened_at=now - timedelta(hours=5))
    trader._paper_staged_ratchet(short, cs, 89.0, s, tf_ms=HOUR, atr_period=14, now_ms=now_ms)
    assert short.stop_loss_price == pytest.approx(100.0 - 0.21)
    off = SimpleNamespace(side="long", entry_price=100.0, initial_stop_price=90.0, stop_loss_price=90.0, peak_price=150.0, opened_at=now - timedelta(hours=5))
    trader._paper_staged_ratchet(off, cs, 150.0, config.load_settings(), tf_ms=HOUR, atr_period=14, now_ms=now_ms)
    assert off.stop_loss_price == 90.0  # every stage off: nothing
