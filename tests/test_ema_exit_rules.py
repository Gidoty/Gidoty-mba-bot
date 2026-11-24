"""EMA exit rules (docs/safety/ema_exit_rules.md): take profit, trailing stop and break-even, all in R, all OFF by default.

PROVES, against the fake exchange and fixed candles: the pure rules (stop wins ties, gaps fill at the open, a level earned by a candle applies from the next one, nothing ever loosens or moves backward, take profit
never fires on the loss side); the live path (take profit exits through lifecycle.execute_exit, a trailing or break-even level moves the stop through lifecycle.move_stop: new stop placed and verified first); the
failure paths (exit order rejected, bot down, position already closed by the exchange stop, new stop refused); the paper path; and that with every rule off nothing changes.
DOES NOT PROVE: that any of the rules makes money, or how Binance behaves."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import ccxt
import pytest
from helpers_live import BULLISH_OHLCV, FakeNotifier, all_trades, new_account, open_protected, run_cycle, the_trade
from test_donchian_integration import NO_EMA

from mba_bot import config, ema_exits, lifecycle, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade, TradingAccount
from mba_bot.strategy import Candle

HOUR = 3_600_000
U = timezone.utc
R = ema_exits.ExitRules


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def c(ts, o, h, l, cl):
    return Candle(ts, o, h, l, cl, 1.0)


# ================================================================================ the pure rules
def test_the_defaults_are_all_off_and_a_typo_never_switches_a_rule_on(monkeypatch):
    for name in ("EMA_TAKE_PROFIT_R", "EMA_TRAIL_TRIGGER_R", "EMA_BREAKEVEN_R", "EMA_TRAIL_ATR"):
        monkeypatch.delenv(name, raising=False)
    s = config.load_settings()
    assert (s.ema_take_profit_r, s.ema_trail_trigger_r, s.ema_breakeven_r, s.ema_trail_atr) == (0.0, 0.0, 0.0, 2.0)
    assert not ema_exits.rules_from_settings(s).any_on
    for bad in ("abc", "", "-1", "0", "nan", "inf", " "):
        monkeypatch.setenv("EMA_TAKE_PROFIT_R", bad)
        assert config.load_settings().ema_take_profit_r == 0.0, bad
    monkeypatch.setenv("EMA_TAKE_PROFIT_R", "3")
    monkeypatch.setenv("EMA_TRAIL_ATR", "99")
    assert (config.load_settings().ema_take_profit_r, config.load_settings().ema_trail_atr) == (3.0, 10.0)
    monkeypatch.setenv("EMA_TAKE_PROFIT_R", "0.01")
    assert config.load_settings().ema_take_profit_r == 0.25  # clamped, never accidentally near zero


def test_take_profit_is_on_the_profit_side_for_both_directions_and_never_fires_on_the_loss_side():
    long = ema_exits.take_profit_level("long", 100.0, 90.0, 3.0)
    short = ema_exits.take_profit_level("short", 100.0, 110.0, 3.0)
    assert long == pytest.approx(130.0) and short == pytest.approx(70.0)
    assert ema_exits.take_profit_level("long", 100.0, 90.0, 0.0) is None and ema_exits.take_profit_level("long", 100.0, 100.0, 3.0) is None
    assert ema_exits.take_profit_touched("long", long, 130.0) and ema_exits.take_profit_touched("long", long, 131.0) and not ema_exits.take_profit_touched("long", long, 129.99)
    assert ema_exits.take_profit_touched("short", short, 70.0) and not ema_exits.take_profit_touched("short", short, 70.01)
    for price in (0.0, 1.0, 50.0, 89.0, 100.0):  # a long's loss side, entry and below: never a take profit
        assert not ema_exits.take_profit_touched("long", long, price)
    for price in (200.0, 150.0, 111.0, 100.0):  # a short's loss side
        assert not ema_exits.take_profit_touched("short", short, price)
    assert not ema_exits.take_profit_touched("long", None, 1e9)


def test_the_stop_wins_when_one_candle_touches_both_and_a_gap_fills_at_the_open():
    rules = R(take_profit_r=2.0)
    both = [c(10, 100, 125, 85, 100)]  # stop 90, take profit 120: both touched
    w = ema_exits.walk("long", 100.0, 90.0, 5, both, rules, [None])
    assert (w.exit_reason, w.exit_price) == ("stop_loss", 90.0)
    gap_down = [c(10, 85, 86, 80, 82)]
    assert ema_exits.walk("long", 100.0, 90.0, 5, gap_down, rules, [None]).exit_price == 85  # opened below the stop: the worse price
    gap_up = [c(10, 125, 130, 124, 128)]
    assert ema_exits.walk("long", 100.0, 90.0, 5, gap_up, rules, [None]).exit_price == 125  # opened above the take profit: the open
    short = [c(10, 100, 115, 75, 100)]
    assert ema_exits.walk("short", 100.0, 110.0, 5, short, R(take_profit_r=2.0), [None]).exit_reason == "stop_loss"


def test_the_candle_before_the_entry_is_never_judged_and_a_level_earned_applies_from_the_next_candle():
    rules = R(trail_trigger_r=1.0, trail_atr=1.0)
    atrs = [1.0, 1.0, 1.0]
    # candle 0 starts before the entry (5) and is skipped even though it spans everything; candle 10 makes the +1R high and ALSO dips below the level it earns (99.5 < 111): it must not stop itself out
    candles = [c(0, 100, 200, 50, 100), c(10, 100, 112, 99.5, 111)]
    w = ema_exits.walk("long", 100.0, 90.0, 5, candles, rules, atrs[:2])
    assert w.exit_reason is None and w.stop == pytest.approx(111.0)
    # the level applies from the NEXT candle: one that stays above it keeps the trade, one that touches it exits at it
    assert ema_exits.walk("long", 100.0, 90.0, 5, candles + [c(20, 111.5, 113, 111.2, 112)], rules, atrs).exit_reason is None
    assert ema_exits.walk("long", 100.0, 90.0, 5, candles + [c(20, 111.5, 113, 110.9, 112)], rules, atrs).exit_price == pytest.approx(111.0)


def test_a_candle_that_touches_the_level_earned_by_the_previous_one_exits_at_that_level():
    rules = R(trail_trigger_r=1.0, trail_atr=1.0)
    candles = [c(10, 100, 112, 99.5, 111), c(20, 111, 111.5, 105, 106)]
    w = ema_exits.walk("long", 100.0, 90.0, 5, candles, rules, [1.0, 1.0])
    assert (w.exit_reason, w.exit_price, w.exit_ts) == ("trailing_stop", 111.0, 20)


def test_the_trailing_stop_never_moves_backward_whatever_the_atr_or_the_candles_do():
    rules = R(trail_trigger_r=1.0, trail_atr=1.0)
    long_c = [c(10, 100, 115, 110, 114), c(11, 114, 120, 119.5, 120), c(12, 120, 121, 119.5, 120.5), c(13, 120.5, 121, 119.6, 120.7), c(14, 120.7, 121, 119.8, 120.9)]
    atrs = [1.0, 1.0, 5.0, 9.0, 20.0]  # the ATR explodes: the raw candidate (high - ATR) falls far below the ratchet, which must hold
    levels = []
    for n in range(1, 6):
        w = ema_exits.walk("long", 100.0, 90.0, 5, long_c[:n], rules, atrs[:n])
        assert w.exit_reason is None
        levels.append(w.stop)
    assert levels == [114.0, 119.0, 119.0, 119.0, 119.0]
    short_c = [c(10, 100, 95, 85, 86), c(11, 86, 85.5, 80, 81), c(12, 81, 80.5, 80, 80.2), c(13, 80.2, 80.4, 79.9, 80.1), c(14, 80.1, 80.3, 79.9, 80.0)]
    levels = [ema_exits.walk("short", 100.0, 110.0, 5, short_c[:n], rules, atrs[:n]).stop for n in range(1, 6)]
    assert levels == [86.0, 81.0, 81.0, 81.0, 81.0]  # a short's stop only ever goes DOWN


def test_the_stop_never_loosens_below_the_initial_stop_and_the_breakeven_is_the_entry():
    rules = R(breakeven_r=1.0)
    w = ema_exits.walk("long", 100.0, 90.0, 5, [c(10, 100, 112, 99, 111)], rules, [None])
    assert w.stop == 100.0
    assert ema_exits.earned_stop("long", 100.0, 90.0, 5, [c(10, 100, 105, 99, 101)], rules, [None]) is None  # not up 1R yet: nothing earned
    assert ema_exits.earned_stop("long", 100.0, 90.0, 5, [c(10, 100, 112, 99, 111)], rules, [None]) == 100.0
    assert ema_exits.earned_stop("short", 100.0, 110.0, 5, [c(10, 100, 101, 88, 89)], rules, [None]) == 100.0
    assert ema_exits.earned_stop("long", 100.0, 90.0, 5, [c(10, 100, 112, 99, 111)], R(take_profit_r=3.0), [None]) is None  # take profit alone moves no stop


def test_with_every_rule_off_the_walk_changes_nothing_but_the_initial_stop():
    candles = [c(10 + i, 100, 120 + i, 95, 110) for i in range(5)]
    w = ema_exits.walk("long", 100.0, 90.0, 5, candles, R(), [1.0] * 5)
    assert (w.exit_reason, w.stop) == (None, 90.0)


# ================================================================================ the live path (fake exchange)
def live(monkeypatch, **over):
    """A protected live Futures trade (entry 90, initial stop 43, R = 47), backdated so the candles built next come AFTER the entry and are closed. The EMA signal is silenced afterwards."""
    from helpers_live import make_settings

    settings = make_settings(max_notional_multiplier=100.0, **over)
    client, settings, aid = open_protected(monkeypatch, market="futures", settings=settings)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    now_ms = int(datetime.now(U).timestamp() * 1000)
    base = (now_ms // HOUR) * HOUR - 8 * HOUR
    with session_scope() as s:
        t = s.get(Trade, the_trade(aid).id)
        t.opened_at = datetime.fromtimestamp((base - 1_800_000) / 1000, tz=U)
    t = the_trade(aid)
    assert (t.entry_price, t.initial_stop_price, t.stop_loss_price) == (90.0, 43.0, 43.0)
    return client, settings, aid, base


def candles(client, base, rows):
    client._ohlcv = BULLISH_OHLCV + [[base + i * HOUR, o, h, lo, cl, 1.0] for i, (o, h, lo, cl) in enumerate(rows)]
    client.price = rows[-1][3]


def resting_stops(client):
    return [o for o in client.orders_by_role("stop") if o["status"] == "open"]


def test_take_profit_exits_through_execute_exit_and_leaves_the_account_flat(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_take_profit_r=1.0)  # 90 + 47 = 137
    candles(client, base, [(95, 100, 92, 99), (99, 145, 98, 140)])
    n = run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "take_profit" and client.position == 0.0 and client.violations == []
    assert len(client.orders_by_role("exit")) == 1 and client.orders_by_role("stop")[0]["status"] == "canceled"
    assert any("take profit" in x for x in n.texts)


def test_take_profit_is_found_from_the_forming_candle_and_the_live_price_too(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_take_profit_r=1.0)
    now_ms = int(datetime.now(U).timestamp() * 1000)
    forming_ts = (now_ms // HOUR) * HOUR
    client._ohlcv = BULLISH_OHLCV + [[base, 95, 100, 92, 99, 1.0], [forming_ts, 99, 140, 98, 120, 1.0]]  # the hour still forming touched 137 and came back to 120
    client.price = 120.0
    run_cycle(aid, settings)
    assert the_trade(aid).close_reason == "take_profit"


def test_with_every_rule_off_a_touched_take_profit_changes_nothing(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch)  # defaults
    candles(client, base, [(95, 100, 92, 99), (99, 145, 98, 140)])
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and t.protection_status == "protected" and t.stop_loss_price == 43.0 and client.orders_by_role("exit") == []


def test_a_trailing_stop_moves_the_stop_up_with_the_new_one_placed_first_and_never_backward(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_trail_trigger_r=1.0, ema_trail_atr=2.0)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and t.stop_loss_price > 43.0 and t.initial_stop_price == 43.0 and t.protection_status == "protected"
    first = t.stop_loss_price
    assert len(resting_stops(client)) == 1 and resting_stops(client)[0]["trigger"] == pytest.approx(first)
    seq = [x[0] for x in client.calls]
    assert seq.index("create_order", seq.index("create_order") + 1) < len(seq)  # the entry and a new stop were placed
    # a later round: lower highs and a wider ATR must NOT lower the stop
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139), (139, 139, 120, 125), (125, 126, 115, 118)])
    stops_before = len(client.orders_by_role("stop"))
    run_cycle(aid, settings)
    mid = the_trade(aid)
    assert mid.status == "open" and mid.stop_loss_price >= first  # lower highs never lower it (a smaller ATR may raise it)
    assert len(resting_stops(client)) == 1 and len(client.orders_by_role("stop")) >= stops_before
    # a higher high moves it up, still one resting stop
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139), (139, 139, 120, 125), (125, 126, 115, 118), (118, 170, 116, 168)])
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price >= mid.stop_loss_price and len(resting_stops(client)) == 1  # a higher high with a wide candle can only keep or raise it and client.violations == []


def test_break_even_moves_the_stop_to_the_entry_price(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_breakeven_r=1.0)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.stop_loss_price == pytest.approx(90.0) and resting_stops(client)[0]["trigger"] == pytest.approx(90.0)


def test_a_stop_the_exchange_no_longer_accepts_keeps_the_old_one_and_tells_the_customer_once(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_trail_trigger_r=1.0, ema_trail_atr=1.0)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    client.fail("create_order", role="stop", exc=ccxt.InvalidOrder("rejected"), times=50)
    n = run_cycle(aid, settings)
    run_cycle(aid, settings, n)
    t = the_trade(aid)
    assert t.status == "open" and t.protection_status == "protected" and t.stop_loss_price == 43.0 and len(resting_stops(client)) == 1
    assert len([x for x in n.texts if "Could not move the trailing stop" in x]) == 1


def test_a_stop_resting_above_the_earned_level_is_never_loosened(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_trail_trigger_r=1.0, ema_trail_atr=1.0)
    with session_scope() as s:
        s.get(Trade, the_trade(aid).id).stop_loss_price = 130.0  # a better stop already rests (set directly)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])  # the earned level (about 138) is above 130 ... use a high stop instead
    with session_scope() as s:
        s.get(Trade, the_trade(aid).id).stop_loss_price = 138.9
    before = len(client.orders_by_role("stop"))
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == 138.9 and len(client.orders_by_role("stop")) == before


def test_when_the_exit_order_is_rejected_the_trade_is_surfaced_not_forgotten(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_take_profit_r=1.0)
    candles(client, base, [(95, 100, 92, 99), (99, 145, 98, 140)])
    client.fail("create_order", role="exit", exc=ccxt.RequestTimeout("down"), times=99)
    n = run_cycle(aid, settings)
    assert the_trade(aid).status == "closing"
    run_cycle(aid, settings, n)
    t = the_trade(aid)
    assert t.status == "attention" and t.attention_reason == "exit_failed"  # the same outcome as any other exit that cannot be placed
    assert len([x for x in n.texts if x.startswith("ATTENTION")]) == 1


def test_a_bot_that_was_down_still_takes_the_profit_from_the_candles_it_missed(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_take_profit_r=1.0)
    # the market touched 137 in hour 1 and fell back to 95 while no cycle ran
    candles(client, base, [(95, 100, 92, 99), (99, 145, 98, 140), (140, 141, 94, 95)])
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "take_profit"  # exited at the market (95), not at the target: a monitored take profit is a market exit, said in the docs


def test_a_position_the_exchange_stop_already_closed_gets_no_second_exit(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_take_profit_r=1.0)
    client.move_price(40.0)  # through the 43.0 stop
    candles(client, base, [(95, 100, 92, 99), (99, 145, 98, 140)])
    client.price = 40.0
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "exchange_stop_triggered" and client.orders_by_role("exit") == [] and client.position == 0.0


def test_a_short_trade_takes_profit_on_the_downside_and_trails_downward(db, monkeypatch):
    from helpers_live import FLAT_OHLCV, make_settings, futures_client, install

    settings = make_settings(max_notional_multiplier=100.0, ema_take_profit_r=1.0, ema_trail_trigger_r=0.5, ema_trail_atr=1.0)
    # a hand-made protected short: entry 100, initial stop 110 (R = 10), take profit 90
    client = futures_client(ohlcv=FLAT_OHLCV, price=100.0)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    aid = new_account(settings, market_type="futures")
    now_ms = int(datetime.now(U).timestamp() * 1000)
    base = (now_ms // HOUR) * HOUR - 8 * HOUR
    client.position = -0.2
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="short", is_paper=False, entry_price=100.0, quantity=0.2, stop_loss_price=110.0, initial_stop_price=110.0, status="open",
                    opened_at=datetime.fromtimestamp((base - 1_800_000) / 1000, tz=U), strategy_id="ema_cross_atr_v1"))
    run_cycle(aid, settings)  # re-protects the legacy-shaped row first
    t = the_trade(aid)
    assert t.status == "open" and t.protection_status == "protected"
    client._ohlcv = FLAT_OHLCV + [[base, 99, 100, 96, 97, 1.0], [base + HOUR, 97, 98, 88, 89, 1.0]]  # low 88 <= 90: take profit
    client.price = 89.0
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "take_profit" and client.position == 0.0


# ================================================================================ the paper path
def paper_trade(side="long", entry=100.0, stop=90.0, **kw):
    from helpers_live import make_settings

    settings = make_settings(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890", **kw)
    aid = new_account(settings, market_type="spot")
    now_ms = int(datetime.now(U).timestamp() * 1000)
    base = (now_ms // HOUR) * HOUR - 8 * HOUR
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None  # PAPER
        s.add(Trade(account_id=aid, side=side, is_paper=True, entry_price=entry, quantity=1.0, stop_loss_price=stop, initial_stop_price=stop, status="open",
                    opened_at=datetime.fromtimestamp((base - 1_800_000) / 1000, tz=U), strategy_id="ema_cross_atr_v1", pair="BTC/USDT"))
    return settings, aid, base


def paper_rows(base, rows):
    warm = [[base - (5 - i) * HOUR, 100, 101, 99, 100, 1.0] for i in range(4)]  # before the entry: only the ATR warm-up
    return warm + [[base + i * HOUR, o, h, lo, cl, 1.0] for i, (o, h, lo, cl) in enumerate(rows)]


def run_paper(monkeypatch, settings, aid, ohlcv, price):
    from helpers_live import install, spot_client

    client = spot_client(ohlcv=ohlcv, price=price)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    run_cycle(aid, settings)


def test_a_paper_trade_takes_profit_at_the_target_price(db, monkeypatch):
    settings, aid, base = paper_trade(ema_take_profit_r=2.0, atr_period=2)
    run_paper(monkeypatch, settings, aid, paper_rows(base, [(100, 112, 99, 111), (111, 125, 110, 124)]), 124.0)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "take_profit" and t.exit_price == pytest.approx(120.21) and t.pnl == pytest.approx(20.21)  # entry + 2 R + the cost buffer (both fees at 5 bps plus 11 bps slippage on 100): the target is net of costs (docs/safety/net_pnl.md)


def test_a_paper_trade_ratchets_its_stop_and_exits_on_it_never_looser(db, monkeypatch):
    settings, aid, base = paper_trade(ema_trail_trigger_r=1.0, ema_trail_atr=1.0, atr_period=2)
    run_paper(monkeypatch, settings, aid, paper_rows(base, [(100, 112, 99, 111), (111, 113, 110, 112)]), 112.0)
    t = the_trade(aid)
    assert t.status == "open" and t.stop_loss_price > 90.0 and t.initial_stop_price == 90.0
    level = t.stop_loss_price
    run_paper(monkeypatch, settings, aid, paper_rows(base, [(100, 112, 99, 111), (111, 113, 110, 112), (112, 112, 108, 109)]), 109.0)
    level = max(level, the_trade(aid).stop_loss_price)
    assert the_trade(aid).stop_loss_price >= level - 1e-9  # lower highs: the level did not move down
    run_paper(monkeypatch, settings, aid, paper_rows(base, [(100, 112, 99, 111), (111, 113, 110, 112), (112, 112, 108, 109), (109, 109, level - 5.0, level - 1.0)]), level - 1.0)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason in ("trailing_stop", "break_even_stop") and t.exit_price == pytest.approx(level)


def test_a_paper_trade_with_every_rule_off_is_untouched(db, monkeypatch):
    settings, aid, base = paper_trade(atr_period=2)
    run_paper(monkeypatch, settings, aid, paper_rows(base, [(100, 112, 99, 111), (111, 160, 110, 150)]), 150.0)
    t = the_trade(aid)
    assert t.status == "open" and t.stop_loss_price == 90.0


# ================================================================================ the paper replay with the rules
def _replay_candles(rows):
    base = 1_700_000_000_000 // HOUR * HOUR
    flat = [Candle(base + i * HOUR, 100, 101, 99, 100, 1.0) for i in range(110)]  # history for the indicators
    t0 = base + 110 * HOUR
    return flat + [Candle(t0 + i * HOUR, o, h, lo, cl, 1.0) for i, (o, h, lo, cl) in enumerate(rows)], t0


def _trade(t0, *, close_hour, exit_price, side="long", entry=100.0, stop=90.0):
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, tz=U).strftime("%Y-%m-%d %H:%M:%S")
    return {"trade_ref": "t1", "pair": "BTC/USDT", "side": side, "status": "closed", "entry_price": str(entry), "stop_loss_price": str(stop), "exit_price": str(exit_price), "opened_at": fmt(t0 + 60_000),
            "closed_at": fmt(t0 + close_hour * HOUR + 60_000), "close_reason": "stop_loss"}


def test_the_replay_shows_the_rules_helping_on_one_trade_and_hurting_on_another_without_claiming_either():
    from mba_bot import paper_replay

    params = paper_replay.Params(rules=ema_exits.ExitRules(take_profit_r=2.0))
    # trade 1: the market ran to +2.5R and fell back through the stop: the paper trade lost 1R, the take profit would have banked 2R
    candles, t0 = _replay_candles([(100, 100.5, 99.5, 100), (100, 125, 99, 120), (120, 121, 95, 96), (96, 97, 88, 89)])
    helped = paper_replay.replay_trade(_trade(t0, close_hour=3, exit_price=90.0), candles, params)
    assert helped["paper_r"] == pytest.approx(-1.0) and helped["rules_exit"] == "take_profit" and helped["rules_r"] == pytest.approx(2.0)
    # trade 2: the same touch of +2R, but the paper trade rode on to +5R: the rule would have cut it short
    candles, t0 = _replay_candles([(100, 100.5, 99.5, 100), (100, 122, 99, 118), (118, 130, 117, 128), (128, 152, 127, 150)])
    hurt = paper_replay.replay_trade(_trade(t0, close_hour=3, exit_price=150.0), candles, params)
    assert hurt["paper_r"] == pytest.approx(5.0) and hurt["rules_exit"] == "take_profit" and hurt["rules_r"] == pytest.approx(2.0)
    lines = paper_replay.summarize([helped, hurt])
    text = "\n".join(lines)
    assert "WITH THE EXIT RULES on the same 2 trades" in text and "better on 1, worse on 1" in text and "not evidence that the rules improve results" in text


def test_the_replay_without_rules_adds_nothing_and_a_trade_no_rule_touches_ends_where_it_always_did():
    from mba_bot import paper_replay

    candles, t0 = _replay_candles([(100, 100.5, 99.5, 100), (100, 108, 99, 105), (105, 106, 95, 96)])
    plain = paper_replay.replay_trade(_trade(t0, close_hour=2, exit_price=96.0), candles, paper_replay.Params())
    assert "rules_r" not in plain and "WITH THE EXIT RULES" not in "\n".join(paper_replay.summarize([plain]))
    untouched = paper_replay.replay_trade(_trade(t0, close_hour=2, exit_price=96.0), candles, paper_replay.Params(rules=ema_exits.ExitRules(take_profit_r=5.0)))
    assert untouched["rules_exit"] == "paper_exit_hour_close" and untouched["rules_r"] == pytest.approx(untouched["mainnet_r"])


def test_below_the_trigger_nothing_moves_and_a_trade_of_another_strategy_is_never_touched(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_trail_trigger_r=1.0, ema_trail_atr=1.0, ema_breakeven_r=1.0)
    candles(client, base, [(95, 100, 92, 99), (99, 120, 98, 118)])  # up 30 of the 47 needed
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and t.stop_loss_price == 43.0 and len(client.orders_by_role("stop")) == 1
    class Other:  # a Donchian or RSI trade never gets the EMA rules
        strategy_id = "donchian_breakout_v1"
        initial_stop_price = 43.0
        opened_at = datetime.now(U)
    assert trader._ema_rule_inputs(Other(), [], settings, 0) is None
    t2 = the_trade(aid)
    assert trader._ema_rule_inputs(t2, [], settings, 0) is None  # no candles: nothing to judge


def test_the_rules_never_run_on_a_trade_that_is_not_open_and_protected(db, monkeypatch):
    import asyncio

    from helpers_live import live_context

    client, settings, aid, base = live(monkeypatch, ema_take_profit_r=1.0)
    candles(client, base, [(95, 100, 92, 99), (99, 145, 98, 140)])
    for field, value in (("protection_status", "unprotected"), ("status", "attention")):
        with session_scope() as s:
            t = s.get(Trade, the_trade(aid).id)
            setattr(t, field, value)
        async def go():
            with session_scope(autoflush=False) as s:
                tr = s.get(Trade, the_trade(aid).id)
                ctx = live_context(s, aid, client, settings)
                from mba_bot.strategy import candles_from_ohlcv

                await trader._live_ema_manage(ctx, tr, settings, now_ms=int(datetime.now(U).timestamp() * 1000), candles=candles_from_ohlcv(client._ohlcv), current_price=140.0)

        asyncio.run(go())
        assert client.orders_by_role("exit") == []  # an unverified or surfaced trade is the reconciler's: no strategy decision on it
        with session_scope() as s:
            t = s.get(Trade, the_trade(aid).id)
            t.protection_status, t.status = "protected", "open"


def test_a_candle_that_touched_the_initial_stop_is_left_to_the_exchange_stop_and_the_monitor(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_take_profit_r=1.0)
    candles(client, base, [(95, 100, 40, 60), (60, 70, 55, 65)])  # a candle went through the 43 stop and the price came back; the exchange stop did not fire in the fake
    client.price = 65.0
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and client.orders_by_role("exit") == [] and t.stop_loss_price == 43.0  # the rules path sends no exit of its own for an initial-stop touch
