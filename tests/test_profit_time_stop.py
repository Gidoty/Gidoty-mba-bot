"""Part D: a maximum holding time per strategy (docs/safety/profit_time_stop.md). Off by default.

PROVES: the pure decision (not reached, flat or loss exits, in profit net of costs tightens and never below entry plus the buffer, both directions), the settings (off, typos never switch it on), the live EMA and Donchian
paths (exit through execute_exit with the reason time_stop; a tightened stop through move_stop; nothing before the limit or with the limit off), the paper path, and that RSI keeps its own time stop.
DOES NOT PROVE: that any limit makes money (Part H measures it)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncio

import pytest
from helpers_live import run_cycle, the_trade
from helpers_profit import HOUR, candles, live, resting_stops

from mba_bot import config, profit_protect as pp, trader
from mba_bot.db import init_db

U = timezone.utc


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def T(**o):
    base = dict(side="long", entry=100.0, price=101.0, peak=101.0, atr=2.0, held_candles=10.0, limit_candles=10, buffer=0.21, tighten_atr=1.0)
    base.update(o)
    return pp.time_decision(**base)


# ---------------------------------------------------------------------------------------------- the decision
def test_nothing_happens_while_the_limit_is_off_or_not_reached_and_it_is_reached_at_exactly_the_limit():
    assert T(limit_candles=0) is None and T(limit_candles=-3) is None
    assert T(held_candles=9.99) is None
    assert T(held_candles=10.0) is not None and T(held_candles=50.0) is not None


def test_a_trade_that_is_flat_or_in_loss_net_of_costs_exits_and_one_in_profit_is_tightened():
    assert T(price=95.0, peak=96.0) == ("exit", None)  # a loss
    assert T(price=100.0) == ("exit", None)  # flat
    assert T(price=100.21) == ("exit", None)  # exactly at entry + buffer: not yet in profit NET of costs
    assert T(price=100.22, peak=100.22)[0] == "tighten"
    assert T(side="short", price=105.0, peak=104.0) == ("exit", None)
    assert T(side="short", price=99.79) == ("exit", None)
    assert T(side="short", price=99.78, peak=99.78)[0] == "tighten"


def test_the_tightened_level_is_the_best_price_minus_n_atr_and_never_below_entry_plus_the_buffer():
    assert T(price=110.0, peak=112.0, atr=2.0, tighten_atr=1.0) == ("tighten", pytest.approx(110.0))  # 112 - 2
    assert T(price=110.0, peak=112.0, atr=2.0, tighten_atr=3.0) == ("tighten", pytest.approx(106.0))  # 112 - 6
    assert T(price=101.0, peak=101.0, atr=20.0) == ("tighten", pytest.approx(100.21))  # a huge ATR: the floor, never below zero net
    assert T(price=101.0, peak=101.0, atr=None) == ("tighten", pytest.approx(100.21))
    assert T(side="short", price=88.0, peak=87.0, atr=2.0, tighten_atr=1.0) == ("tighten", pytest.approx(89.0))
    assert T(side="short", price=99.0, peak=99.0, atr=30.0) == ("tighten", pytest.approx(99.79))
    for atr in (0.5, 1.0, 3.0, 40.0):
        for price in (100.5, 105.0, 130.0):
            assert T(price=price, peak=price + 1.0, atr=atr)[1] >= 100.21
    assert T(price=110.0, peak=100.0, atr=2.0)[1] == pytest.approx(108.0)  # a stale low peak never drags the level below the price's own best


def test_the_settings_default_off_and_a_typo_never_switches_a_time_stop_on(monkeypatch):
    for n in ("EMA_MAX_HOLD_CANDLES", "DONCHIAN_MAX_HOLD_CANDLES", "PROFIT_TIME_TIGHTEN_ATR"):
        monkeypatch.delenv(n, raising=False)
    s = config.load_settings()
    assert (s.ema_max_hold_candles, s.donchian_max_hold_candles, s.profit_time_tighten_atr) == (0, 0, 1.0)
    for n in ("EMA_MAX_HOLD_CANDLES", "DONCHIAN_MAX_HOLD_CANDLES"):
        for bad in ("abc", "", "-5", "0", "1.5", "nan", " ", "6h"):
            monkeypatch.setenv(n, bad)
            s = config.load_settings()
            assert (s.ema_max_hold_candles, s.donchian_max_hold_candles) == (0, 0), (n, bad)
        monkeypatch.delenv(n)
    monkeypatch.setenv("EMA_MAX_HOLD_CANDLES", "48")
    monkeypatch.setenv("DONCHIAN_MAX_HOLD_CANDLES", "99999")
    monkeypatch.setenv("PROFIT_TIME_TIGHTEN_ATR", "abc")
    s = config.load_settings()
    assert (s.ema_max_hold_candles, s.donchian_max_hold_candles, s.profit_time_tighten_atr) == (48, 10_000, 1.0)


# ---------------------------------------------------------------------------------------------- live, EMA (opened 8.5 candles ago, entry 90)
def test_a_flat_trade_at_the_limit_exits_through_execute_exit_with_the_reason_time_stop(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_max_hold_candles=6)
    candles(client, base, [(95, 100, 92, 99), (99, 100, 89, 90.1)])  # price 90.1: above the entry but not beyond entry + buffer (90.19): flat net of costs
    n = run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "time_stop" and client.position == 0.0 and len(client.orders_by_role("exit")) == 1 and client.orders_by_role("stop")[0]["status"] == "canceled"
    assert any("time stop" in x for x in n.texts)


def test_a_losing_trade_at_the_limit_exits_too(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_max_hold_candles=6)
    candles(client, base, [(95, 100, 92, 99), (99, 100, 70, 75)])
    run_cycle(aid, settings)
    assert the_trade(aid).close_reason == "time_stop"


def test_a_trade_in_profit_at_the_limit_is_tightened_through_move_stop_not_closed(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_max_hold_candles=6)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and 90.0 + 0.189 <= t.stop_loss_price < 139.0 and t.initial_stop_price == 43.0  # tightened: never below entry + buffer, never at or above the price
    assert len(resting_stops(client)) == 1 and resting_stops(client)[0]["trigger"] == pytest.approx(t.stop_loss_price) and client.violations == [] and len(client.orders_by_role("exit")) == 0


def test_nothing_happens_before_the_limit_or_with_the_limit_off(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_max_hold_candles=12)  # opened 8.5 to 9.5 candles ago, depending on the minute of the hour
    candles(client, base, [(95, 100, 92, 99), (99, 100, 89, 91)])
    run_cycle(aid, settings)
    assert the_trade(aid).status == "open" and the_trade(aid).stop_loss_price == 43.0


def test_with_the_limit_off_a_long_held_trade_is_untouched(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch)
    candles(client, base, [(95, 100, 92, 99), (99, 100, 89, 91)])
    run_cycle(aid, settings)
    assert the_trade(aid).status == "open" and the_trade(aid).stop_loss_price == 43.0 and len(client.orders_by_role("exit")) == 0


# ---------------------------------------------------------------------------------------------- live, Donchian
def test_a_live_donchian_trade_at_the_limit_exits_with_time_stop(db, monkeypatch):
    from test_live_donchian import _later, live_account, setup

    settings, client = setup(monkeypatch, donchian_max_hold_candles=3)
    aid = live_account(settings)
    run_cycle(aid, settings)
    assert the_trade(aid).status == "open"
    _later(aid, 5.5)  # held 5.5 candles; the price is about where it entered
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "time_stop" and client.position == 0.0


def test_a_live_donchian_trade_before_the_limit_is_left_alone(db, monkeypatch):
    from test_live_donchian import _later, live_account, setup

    settings, client = setup(monkeypatch, donchian_max_hold_candles=30)
    aid = live_account(settings)
    run_cycle(aid, settings)
    _later(aid, 5.5)
    run_cycle(aid, settings)
    assert the_trade(aid).status == "open"


def test_rsi_keeps_its_own_time_stop_and_the_new_limits_do_not_touch_it(db, monkeypatch):
    import inspect

    assert "ema_max_hold_candles" not in inspect.getsource(trader._live_rsi_exit) + inspect.getsource(trader._live_rsi_rules)
    assert config.load_settings().rsi_mr_time_stop_candles == config.load_settings().rsi_mr_time_stop_candles  # its own setting, unchanged


# ---------------------------------------------------------------------------------------------- paper
class Notifier:
    def __init__(self):
        self.texts = []

    async def send(self, target, text):
        self.texts.append(text)


def paper_bits(**over):
    now = datetime.now(U)
    cs = [SimpleNamespace(timestamp=int(now.timestamp() * 1000) - (30 - i) * HOUR, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0) for i in range(30)]
    t = SimpleNamespace(side="long", entry_price=100.0, quantity=2.0, initial_stop_price=90.0, stop_loss_price=90.0, peak_price=100.0, opened_at=now - timedelta(hours=10), status="open", pair="BTC/USDT",
                        strategy_id=None, exit_price=None, pnl=None, closed_at=None, close_reason=None)
    for k, v in over.items():
        setattr(t, k, v)
    acct = SimpleNamespace(paper_balance=1000.0, notification_target=None, pair="BTC/USDT", market_type="futures")
    sess = SimpleNamespace(commit=lambda: None)
    s = config.load_settings()
    return now, cs, t, acct, sess, s


def test_a_paper_trade_flat_at_the_limit_is_closed_at_the_price_with_time_stop():
    now, cs, t, acct, sess, s = paper_bits()
    n = Notifier()
    closed = asyncio.run(trader._paper_time_stop(sess, acct, t, cs, 100.0, s, n, tf_ms=HOUR, atr_period=14, now_ms=int(now.timestamp() * 1000), limit=8, now=now))
    assert closed and t.status == "closed" and t.close_reason == "time_stop" and t.exit_price == 100.0 and t.pnl == 0.0 and acct.paper_balance == 1000.0 and "time stop" in n.texts[0]


def test_a_paper_trade_in_profit_has_its_stop_tightened_and_a_price_through_the_level_exits_at_the_price():
    now, cs, t, acct, sess, s = paper_bits(peak_price=112.0)
    n = Notifier()
    closed = asyncio.run(trader._paper_time_stop(sess, acct, t, cs, 110.0, s, n, tf_ms=HOUR, atr_period=14, now_ms=int(now.timestamp() * 1000), limit=8, now=now))
    assert not closed and t.status == "open" and 100.2 <= t.stop_loss_price <= 110.0
    before = t.stop_loss_price
    asyncio.run(trader._paper_time_stop(sess, acct, t, cs, 110.0, s, n, tf_ms=HOUR, atr_period=14, now_ms=int(now.timestamp() * 1000), limit=8, now=now))
    assert t.stop_loss_price == before  # never loosened, never re-set
    short_now, cs2, short, acct2, sess2, s2 = paper_bits(side="short", initial_stop_price=110.0, stop_loss_price=110.0, peak_price=88.0)
    asyncio.run(trader._paper_time_stop(sess2, acct2, short, cs2, 89.0, s2, n, tf_ms=HOUR, atr_period=14, now_ms=int(short_now.timestamp() * 1000), limit=8, now=short_now))
    assert short.status == "open" and 89.0 <= short.stop_loss_price <= 99.79


def test_a_paper_trade_with_the_limit_off_or_not_reached_is_untouched():
    now, cs, t, acct, sess, s = paper_bits()
    n = Notifier()
    for limit in (0, 11):
        assert not asyncio.run(trader._paper_time_stop(sess, acct, t, cs, 100.0, s, n, tf_ms=HOUR, atr_period=14, now_ms=int(now.timestamp() * 1000), limit=limit, now=now))
    assert t.status == "open" and t.stop_loss_price == 90.0 and n.texts == []


def test_a_paper_stop_that_is_already_better_than_the_tightened_level_is_never_loosened():
    now, cs, t, acct, sess, s = paper_bits(peak_price=100.4, stop_loss_price=105.0)
    n = Notifier()
    asyncio.run(trader._paper_time_stop(sess, acct, t, cs, 100.5, s, n, tf_ms=HOUR, atr_period=14, now_ms=int(now.timestamp() * 1000), limit=8, now=now))
    assert t.stop_loss_price == 105.0 and t.status == "open"


def test_a_tightening_worth_less_than_its_exchange_calls_is_not_made(db, monkeypatch):
    from mba_bot.db import session_scope
    from mba_bot.models import Trade

    client, settings, aid, base = live(monkeypatch, ema_max_hold_candles=6)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    level = the_trade(aid).stop_loss_price  # the tightened level that was placed
    with session_scope() as s:  # the stop rests a hair below the level the rule now asks for, and the last move was long ago
        tr = s.get(Trade, the_trade(aid).id)
        tr.stop_loss_price = level - 0.01
        tr.stop_moved_at = None
    placed = len(client.orders_by_role("stop"))
    run_cycle(aid, settings)
    assert len(client.orders_by_role("stop")) == placed and the_trade(aid).stop_loss_price == pytest.approx(level - 0.01)
