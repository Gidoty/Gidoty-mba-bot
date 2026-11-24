"""The +1R profit lock (docs/safety/profit_lock.md). Off by default.

PROVES: the pure level and decision (activation edge, the cost buffer, a level never beyond the best price, the better stop wins, exit when the price is through the level, the every-round call limits, both sides); the
settings (off, typos, clamps, the mode default); that live EMA, Donchian and RSI trades get the lock through lifecycle.move_stop (new stop placed first, never loosened, never on the wrong side) and exits through
execute_exit; that the closed-candle mode ignores the forming candle and the every-round mode does not; the real fee tier and the funding paid in the level; and the paper ratchet.
DOES NOT PROVE: that the lock makes money (the sealed test PR #96 measured it: no consistent gain), or how Binance behaves (UNVERIFIED)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from helpers_live import BULLISH_OHLCV, run_cycle, the_trade
from helpers_profit import HOUR, candles, live, resting_stops

from mba_bot import config, profit_lock as pl, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade

U = timezone.utc


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    trader._LOCK_MOVES.clear()


def LV(**o):
    base = dict(side="long", entry=100.0, initial_stop=90.0, peak=110.0, lock_r=1.0, buffer=0.21)
    base.update(o)
    return pl.lock_level(**base)


def D(**o):
    base = dict(side="long", level=100.21, current_stop=90.0, price=108.0, entry=100.0, atr=2.0, r=10.0, every_round=False, last_move_ms=None, now_ms=10_000_000, min_interval_s=900, min_improve_atr=0.25)
    base.update(o)
    return pl.decide(**base)


# ---------------------------------------------------------------------------------------------- the level
def test_the_level_is_entry_plus_the_buffer_once_the_best_price_is_up_the_set_r():
    assert LV(peak=109.99) is None  # not up 1 R yet
    assert LV(peak=110.0) == pytest.approx(100.21)
    assert LV(lock_r=1.5, peak=114.99) is None and LV(lock_r=1.5, peak=115.0) == pytest.approx(100.21)
    assert LV(side="short", initial_stop=110.0, peak=90.0) == pytest.approx(99.79)  # below the entry for a short
    assert LV(side="short", initial_stop=110.0, peak=90.01) is None
    assert LV(lock_r=0.0) is None and LV(lock_r=-1.0) is None  # off
    for missing in ({"peak": None}, {"initial_stop": None}, {"initial_stop": 100.0}, {"entry": 0.0}):
        assert LV(**missing) is None, missing
    assert LV(buffer=-5.0) == pytest.approx(100.0)  # a negative buffer never lowers the level below the entry


def test_a_level_that_is_not_below_the_best_price_is_never_asked_for():
    assert LV(initial_stop=99.9, peak=100.1, buffer=0.21) is None  # R 0.1, buffer 0.21: the level (100.21) is above the best price
    assert LV(initial_stop=99.9, peak=100.3, buffer=0.21) == pytest.approx(100.21)
    assert LV(side="short", initial_stop=100.1, peak=99.9, buffer=0.21) is None


# ---------------------------------------------------------------------------------------------- the decision
def test_the_better_stop_wins_and_nothing_is_loosened_or_repeated():
    assert D(current_stop=100.21) == ("hold", "not_better")  # already there
    assert D(current_stop=104.0) == ("hold", "not_better")  # a Donchian trail already above the lock level
    assert D(current_stop=100.0) == ("move", "ok")  # a stop at the entry is below entry + buffer: the lock improves it
    assert D(level=None) == ("hold", "no_level") and D(level=0.0) == ("hold", "no_level")
    assert D(side="short", level=99.79, current_stop=105.0, price=95.0) == ("move", "ok")
    assert D(side="short", level=99.79, current_stop=99.0, price=95.0) == ("hold", "not_better")


def test_a_price_at_or_through_the_level_exits_in_both_modes_and_is_never_delayed():
    assert D(price=100.21)[0] == "exit" and D(price=95.0)[0] == "exit" and D(price=100.22)[0] == "move"
    assert D(side="short", level=99.79, current_stop=105.0, price=99.79)[0] == "exit" and D(side="short", level=99.79, current_stop=105.0, price=99.78)[0] == "move"
    assert D(price=100.0, every_round=True, last_move_ms=9_999_999)[0] == "exit"  # the call limits never hold back an exit


def test_the_every_round_mode_bounds_the_exchange_calls():
    r = dict(every_round=True)
    assert D(**r, current_stop=100.0, level=100.21)[1] == "improvement_too_small"  # needs 0.25 x ATR 2 = 0.5
    assert D(**r, current_stop=99.0, level=100.21)[0] == "move"
    assert D(**r, current_stop=100.0, level=100.6, price=108.0, last_move_ms=10_000_000 - 899_000)[1] == "too_soon"
    assert D(**r, current_stop=100.0, level=100.6, price=108.0, last_move_ms=10_000_000 - 900_000)[0] == "move"
    assert D(**r, current_stop=90.0, level=100.21, last_move_ms=10_000_000 - 1_000)[0] == "move"  # the FIRST lock (stop still below the entry) is never delayed
    assert D(**r, current_stop=100.0, level=100.6, atr=None, r=10.0)[1] == "improvement_too_small" and D(**r, current_stop=99.5, level=100.6, atr=None, r=10.0)[0] == "move"
    assert D(every_round=False, current_stop=100.0, level=100.21, last_move_ms=9_999_999)[0] == "move"  # closed-candle mode has no limits: the level changes at most when a candle closes or funding settles


def test_the_settings_are_off_by_default_clamped_and_a_typo_never_switches_the_lock_or_the_fast_mode_on(monkeypatch):
    for n in ("PROFIT_LOCK_R", "PROFIT_LOCK_EVERY_ROUND", "PROFIT_MOVE_MIN_INTERVAL_SECONDS", "PROFIT_MOVE_MIN_IMPROVE_ATR"):
        monkeypatch.delenv(n, raising=False)
    s = config.load_settings()
    assert (s.profit_lock_r, s.profit_lock_every_round, s.profit_move_min_interval_seconds, s.profit_move_min_improve_atr) == (0.0, False, 900, 0.25)
    for bad in ("abc", "", "-1", "0", "nan", "inf", " ", "1,5", "1R"):
        monkeypatch.setenv("PROFIT_LOCK_R", bad)
        assert config.load_settings().profit_lock_r == 0.0, bad
    for bad in ("", "no", "tru", "2", " ", "enabled"):
        monkeypatch.setenv("PROFIT_LOCK_EVERY_ROUND", bad)
        assert config.load_settings().profit_lock_every_round is False, bad
    monkeypatch.setenv("PROFIT_LOCK_R", "0.01")
    assert config.load_settings().profit_lock_r == 0.25
    monkeypatch.setenv("PROFIT_LOCK_R", "1")
    monkeypatch.setenv("PROFIT_LOCK_EVERY_ROUND", "true")
    monkeypatch.setenv("PROFIT_MOVE_MIN_INTERVAL_SECONDS", "1")
    s = config.load_settings()
    assert (s.profit_lock_r, s.profit_lock_every_round, s.profit_move_min_interval_seconds) == (1.0, True, 60)


# ---------------------------------------------------------------------------------------------- live EMA (entry 90, initial stop 43, R = 47, +1R = 137)
CLOSED = [(95, 100, 92, 99)] * 7 + [(99, 101, 95, 100)]  # eight closed hourly candles below +1R


def market(client, base, last, price):
    rows = CLOSED + [last]
    client._ohlcv = BULLISH_OHLCV + [[base + i * HOUR, o, h, lo, cl, 1.0] for i, (o, h, lo, cl) in enumerate(rows)]
    client.price = price


BUF = 90.0 * 21 / 10_000


def test_with_the_lock_off_nothing_moves_however_far_the_price_goes(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch)
    candles(client, base, [(95, 100, 92, 99), (99, 300, 98, 290)])
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == 43.0 and len(client.orders_by_role("stop")) == 1


def test_the_lock_moves_the_stop_to_entry_plus_the_buffer_through_move_stop_once_a_closed_candle_shows_plus_1r(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, profit_lock_r=1.0)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])  # the last candle is the 8th hour ago... closed: its high 140 is +1.06 R
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.stop_loss_price == pytest.approx(90.0 + BUF, abs=0.011) and t.stop_loss_price > 90.0  # at or above zero net, not at the entry
    assert len(resting_stops(client)) == 1 and resting_stops(client)[0]["trigger"] == pytest.approx(t.stop_loss_price) and client.violations == [] and t.initial_stop_price == 43.0
    stops = len(client.orders_by_role("stop"))
    run_cycle(aid, settings)
    assert len(client.orders_by_role("stop")) == stops  # a second round with the same candles places nothing


def test_the_default_closed_candle_mode_ignores_a_spike_in_the_forming_candle_and_the_every_round_mode_does_not(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, profit_lock_r=1.0)
    market(client, base, (100, 150, 99, 148), 148.0)  # the forming hour touched 150 (+1.28 R)
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == 43.0  # closed candles never reached +1R
    import dataclasses

    fast = dataclasses.replace(settings, profit_lock_every_round=True)
    run_cycle(aid, fast)
    t = the_trade(aid)
    assert t.peak_price == 150.0 and t.stop_loss_price == pytest.approx(90.0 + BUF, abs=0.011)  # the running best price counts


def test_a_pullback_through_the_level_before_the_stop_could_move_exits_near_the_entry_through_execute_exit(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, profit_lock_r=1.0)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 100), (100, 100, 90, 90.1)])  # +1.06 R, then back to 90.1: through entry + buffer (90.19)
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "profit_lock" and client.position == 0.0 and len(client.orders_by_role("exit")) == 1
    assert t.exit_price == pytest.approx(90.1, abs=0.2) and client.orders_by_role("stop")[0]["status"] == "canceled"


def test_a_stop_that_already_rests_above_the_level_is_never_loosened_and_a_refused_stop_keeps_the_old_one(db, monkeypatch):
    import ccxt

    client, settings, aid, base = live(monkeypatch, profit_lock_r=1.0)
    with session_scope() as s:
        s.get(Trade, the_trade(aid).id).stop_loss_price = 120.0  # a better stop (a Donchian trail) already rests
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    before = len(client.orders_by_role("stop"))
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == 120.0 and len(client.orders_by_role("stop")) == before
    with session_scope() as s:
        s.get(Trade, the_trade(aid).id).stop_loss_price = 43.0
    client.fail("create_order", role="stop", exc=ccxt.InvalidOrder("rejected"), times=50)
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and t.protection_status == "protected" and t.stop_loss_price == 43.0  # the old stop stays, the position is never without one


def test_the_level_uses_the_accounts_real_taker_rate_and_the_funding_paid_so_far(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, profit_lock_r=1.0)
    client.taker_fee = 0.0004  # a better tier than the assumed 5 bps
    now = int(time.time() * 1000)
    client.funding_rows = [{"timestamp": now - 1000, "amount": -0.30}]  # paid 0.30 on the position
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    t = the_trade(aid)
    expected = 90.0 + 90.0 * (2 * 4.0 + 11.0) / 10_000 + 0.30 / t.quantity
    assert t.stop_loss_price == pytest.approx(expected, abs=0.011) and t.funding_quote == pytest.approx(-0.30)
    assert [x for x in client.calls if x[0] == "fetch_trading_fee"]


def test_with_no_commission_endpoint_the_assumed_rate_is_used_and_funding_received_never_lowers_the_level(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, profit_lock_r=1.0)
    client.funding_rows = [{"timestamp": int(time.time() * 1000) - 1000, "amount": +0.50}]  # received
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == pytest.approx(90.0 + BUF, abs=0.011)


def test_a_failed_fee_or_funding_read_never_blocks_the_lock(db, monkeypatch):
    import ccxt

    client, settings, aid, base = live(monkeypatch, profit_lock_r=1.0)
    client.fail("fetch_trading_fee", exc=ccxt.RequestTimeout("down"), times=9)
    client.fail("fetch_funding_history", exc=ccxt.RequestTimeout("down"), times=9)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == pytest.approx(90.0 + BUF, abs=0.011)


def test_the_every_round_mode_spaces_later_refinements_but_never_the_first_lock(db, monkeypatch):
    import dataclasses

    client, settings, aid, base = live(monkeypatch, profit_lock_r=1.0, profit_lock_every_round=True)
    market(client, base, (100, 150, 99, 148), 148.0)
    run_cycle(aid, settings)
    first = the_trade(aid).stop_loss_price
    assert first == pytest.approx(90.0 + BUF, abs=0.011)
    stops = len(client.orders_by_role("stop"))
    client.funding_rows = [{"timestamp": int(time.time() * 1000) - 500, "amount": -3.0}]  # a big funding payment raises the buffer: a refinement
    with session_scope() as s:
        s.get(Trade, the_trade(aid).id).funding_read_at = None  # re-read now
    run_cycle(aid, settings)
    assert len(client.orders_by_role("stop")) == stops  # too soon after the first move: held (the improvement is also small)
    trader._LOCK_MOVES.clear()


# ---------------------------------------------------------------------------------------------- Donchian and RSI trades, paper
def test_a_live_donchian_trade_gets_the_lock_and_the_better_stop_wins(db, monkeypatch):
    from test_live_donchian import _later, live_account, raw, rising, setup

    settings, client = setup(monkeypatch, profit_lock_r=1.0)
    aid = live_account(settings)
    run_cycle(aid, settings)
    first = the_trade(aid)
    monkeypatch.setattr(trader.donchian_trail, "trailing_level", lambda **k: None)  # isolate the lock from the strategy's own trail
    cs = rising(4)
    client._ohlcv, client.price = raw(cs), cs[-1].close + 0.5
    _later(aid, 5.5)
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.stop_loss_price == pytest.approx(first.entry_price * (1 + 21 / 10_000), abs=0.011) and t.stop_loss_price < client.price and len(resting_stops(client)) == 1 and client.violations == []


def test_a_donchian_trail_already_above_the_lock_level_is_left_alone(db, monkeypatch):
    from test_live_donchian import _later, live_account, raw, rising, setup

    settings, client = setup(monkeypatch, profit_lock_r=1.0)
    aid = live_account(settings)
    run_cycle(aid, settings)
    cs = rising(4)
    client._ohlcv, client.price = raw(cs), cs[-1].close + 0.5
    _later(aid, 5.5)
    run_cycle(aid, settings)  # the strategy's own trail moves first; the lock sees a better stop and holds
    t = the_trade(aid)
    entry_plus_buffer = t.entry_price * (1 + 21 / 10_000)
    assert t.stop_loss_price >= entry_plus_buffer - 0.011
    n = len(client.orders_by_role("stop"))
    run_cycle(aid, settings)
    assert len(client.orders_by_role("stop")) == n and client.violations == []


def test_a_live_rsi_trade_gets_the_lock_too(db, monkeypatch):
    from test_live_rsi import _opened_hours_ago, live_account, raw, setup
    from test_rsi_mr import LONG

    from mba_bot import rsi_mr

    settings, client = setup(monkeypatch, market="futures", profit_lock_r=0.25)
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
    assert t.status == "open" and t.stop_loss_price >= first.entry_price and t.stop_loss_price > first.stop_loss_price and client.violations == []


def cd(ts, h, lo, c):
    return SimpleNamespace(timestamp=ts, high=h, low=lo, open=c, close=c)


def test_the_paper_lock_ratchets_the_stop_never_loosens_and_exits_at_the_price_when_through_the_level():
    import dataclasses

    now = datetime.now(U)
    now_ms = int(now.timestamp() * 1000)
    cs = [cd(now_ms - (30 - i) * HOUR, 101.0, 99.0, 100.0) for i in range(30)]
    on = dataclasses.replace(config.load_settings(), profit_lock_r=1.0)
    t = SimpleNamespace(side="long", entry_price=100.0, initial_stop_price=90.0, stop_loss_price=90.0, peak_price=112.0, opened_at=now - timedelta(hours=40))
    cs[-3] = cd(cs[-3].timestamp, 111.0, 99.0, 100.0)  # a CLOSED candle after the entry reached +1.1 R
    t.opened_at = now - timedelta(hours=5)
    trader._paper_profit_lock(t, cs, 108.0, on)
    assert t.stop_loss_price == pytest.approx(100.0 + 100.0 * 21 / 10_000)
    t.stop_loss_price = 90.0
    trader._paper_profit_lock(t, cs, 100.05, on)  # the price is already below the level: set AT the price
    assert t.stop_loss_price == pytest.approx(100.05)
    t.stop_loss_price = 105.0
    trader._paper_profit_lock(t, cs, 108.0, on)
    assert t.stop_loss_price == 105.0  # never loosened
    short = SimpleNamespace(side="short", entry_price=100.0, initial_stop_price=110.0, stop_loss_price=110.0, peak_price=88.0, opened_at=now - timedelta(hours=5))
    cs2 = [cd(c.timestamp, 101.0, 89.0, 100.0) for c in cs]
    trader._paper_profit_lock(short, cs2, 92.0, on)
    assert short.stop_loss_price == pytest.approx(100.0 - 0.21)
    off = SimpleNamespace(side="long", entry_price=100.0, initial_stop_price=90.0, stop_loss_price=90.0, peak_price=150.0, opened_at=now - timedelta(hours=5))
    trader._paper_profit_lock(off, cs, 150.0, config.load_settings())
    assert off.stop_loss_price == 90.0


def test_with_the_lock_off_a_donchian_trade_is_untouched_by_it(db, monkeypatch):
    from test_live_donchian import _later, live_account, raw, rising, setup

    settings, client = setup(monkeypatch)  # PROFIT_LOCK_R = 0
    aid = live_account(settings)
    run_cycle(aid, settings)
    first = the_trade(aid)
    monkeypatch.setattr(trader.donchian_trail, "trailing_level", lambda **k: None)
    cs = rising(4)
    client._ohlcv, client.price = raw(cs), cs[-1].close + 0.5
    _later(aid, 5.5)
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == first.stop_loss_price and len(client.orders_by_role("stop")) == 1
