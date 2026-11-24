"""B5b: live Donchian breakout with a trailing stop (LIVE_DONCHIAN, default off).

PROVES (fake exchange, fixed candles): the pure trailing level is the ratchet `exit_check` walks (never lowered, the level a touch exits at); `lifecycle.move_stop` moves a protective stop ONLY in the
favourable direction and below the price, places and verifies the NEW stop BEFORE it records it and BEFORE it cancels the old one (call order), keeps the old stop and tells the customer once when the new
one cannot be placed or verified (including Binance refusing a second closePosition stop, and Spot's locked balance), still reports 'moved' when only the old cancel fails, and never touches a trade that is
not open and protected; with the switch off a live account never runs the Donchian code; with it on an opted-in account enters through the same guarded path (strategy_id, initial_stop_price, protective
stop verified), Donchian has priority over EMA for the one live slot, an open Donchian trade trails its stop, exits on a close beyond the opposite channel through lifecycle.execute_exit, and is not
closed by an EMA crossover.
DOES NOT PROVE: that Binance accepts a second stop while one rests (UNVERIFIED: a refusal is handled by keeping the old stop), any profit, or anything else about Binance."""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from helpers_live import FakeNotifier, all_trades, futures_client, install, live_context, make_settings, new_account, open_protected, run_cycle, spot_client, the_trade
from sqlalchemy import create_engine, inspect
from test_donchian import base, cd, with_last
from test_donchian_integration import LONG_ROW, NO_EMA, raw

from mba_bot import donchian as D, donchian_trail as TR, lifecycle, trader
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade, TradingAccount
from mba_bot.strategy import SignalType, StrategySignal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3_600_000
BUY_EMA = StrategySignal(SignalType.BULLISH_CROSSOVER, "bullish crossover", 100.0, 99.0, 1.0, 0.01)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


# ------------------------------------------------------------------ the pure trailing level
def rising(n_extra, *, start=41, step=2.0, atr_range=2.0):
    """40 flat candles, the breakout candle, then n_extra candles that keep making higher highs."""
    cs = with_last(LONG_ROW)
    price = 103.0
    for k in range(n_extra):
        price += step
        cs.append(cd(start + k, price - 1.0, price + 0.5, price - 1.0 - atr_range + 2.0, price, 150.0))
    return cs


def test_the_trailing_level_is_the_highest_high_minus_two_atr_and_ratchets():
    cs = rising(3)
    opened = cs[40].timestamp  # the breakout candle is the entry candle
    level = TR.trailing_level(side="long", entry=103.0, opened_ms=cs[41].timestamp, closed=cs)
    assert level is not None
    atr = D.atr_series([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], 14)
    expected = max(c.high for c in cs[41:]) - 2.0 * atr[-1]
    assert abs(level - expected) < 1e-9 or level >= expected - 1e-9  # the ratchet is never below the latest candidate
    shorter = TR.trailing_level(side="long", entry=103.0, opened_ms=cs[41].timestamp, closed=cs[:-1])
    assert level >= shorter  # more candles never lower it
    assert TR.trailing_level(side="long", entry=103.0, opened_ms=cs[-1].timestamp + HOUR, closed=cs) is None  # nothing closed after the entry yet


def test_the_level_is_the_one_exit_check_exits_at():
    cs = rising(4)
    open_ms = cs[41].timestamp
    level = TR.trailing_level(side="long", entry=103.0, opened_ms=open_ms, closed=cs)
    drop = cs + [cd(45, level + 1.0, level + 1.5, level - 0.5, level + 0.2, 150.0)]  # a candle whose low touches the level
    ex = D.exit_check(side="long", entry=103.0, stop=100.0, opened_ms=open_ms, closed=drop, tf_ms=HOUR)
    assert ex is not None and ex.reason == D.EXIT_TRAIL and abs(ex.price - level) < 1e-9


def test_the_short_level_mirrors_and_never_rises():
    cs = base(40) + [cd(40, 100.0, 100.5, 96.5, 97.0, 200.0)]
    price = 97.0
    for k in range(4):
        price -= 2.0
        cs.append(cd(41 + k, price + 1.0, price + 1.0, price - 0.5, price, 150.0))
    a = TR.trailing_level(side="short", entry=97.0, opened_ms=cs[41].timestamp, closed=cs[:-1])
    b = TR.trailing_level(side="short", entry=97.0, opened_ms=cs[41].timestamp, closed=cs)
    assert a is not None and b is not None and b <= a  # for a short the level only ever goes down


def test_the_level_never_loosens_when_the_atr_grows():
    cs = rising(3)
    open_ms = cs[41].timestamp
    before = TR.trailing_level(side="long", entry=103.0, opened_ms=open_ms, closed=cs)
    wide = cs + [cd(44, 108.0, 109.0, 95.0, 108.5, 150.0)]  # a wide-range candle below the old extreme: ATR jumps, so the raw candidate is LOWER than the level already earned
    after = TR.trailing_level(side="long", entry=103.0, opened_ms=open_ms, closed=wide)
    assert after >= before - 1e-12


# ------------------------------------------------------------------ move_stop
def live_trade(monkeypatch, *, market="futures", **client_kw):
    client, settings, aid = open_protected(monkeypatch, market=market, **client_kw)
    return client, settings, aid


def ctx_for(aid, client, settings, notifier=None):
    session_cm = session_scope(autoflush=False)
    session = session_cm.__enter__()
    return session_cm, session, live_context(session, aid, client, settings, notifier or FakeNotifier())


def run_move(aid, client, settings, *, new_stop, price, notifier=None, repeat=1, prepare=None):
    notifier = notifier or FakeNotifier()
    results = []

    async def go():
        with session_scope(autoflush=False) as session:
            ctx = live_context(session, aid, client, settings, notifier)
            trade = session.get(Trade, the_trade(aid).id)
            await ctx.venue.prepare(managing=True)
            if prepare:
                prepare(ctx)
            for _ in range(repeat):
                results.append(await lifecycle.move_stop(ctx, trade, new_stop=new_stop, current_price=price))

    asyncio.run(go())
    return results, notifier


def resting_stops(client):
    return [o for o in client.orders.values() if o["conditional"] and o["status"] == "open"]


def test_a_favourable_move_places_the_new_stop_first_verifies_it_and_only_then_cancels_the_old(monkeypatch, db):
    client, settings, aid = live_trade(monkeypatch)
    before = the_trade(aid)
    old_stop, old_attempt = before.stop_loss_price, before.protection_attempt
    price = before.entry_price * 1.05
    client.price = price
    calls_before = len(client.calls)
    results, n = run_move(aid, client, settings, new_stop=before.entry_price * 1.01, price=price)
    assert results == ["moved"]
    t = the_trade(aid)
    assert t.status == "open" and t.protection_status == "protected" and t.stop_loss_price > old_stop and t.protection_attempt == old_attempt + 1
    seq = [c[0] for c in client.calls[calls_before:]]
    assert seq.index("create_order") < seq.index("cancel_order")  # the position is never without a resting stop: new first, old cancelled after
    stops = resting_stops(client)
    assert len(stops) == 1 and abs(stops[0]["trigger"] - t.stop_loss_price) < 1e-6  # exactly one stop rests, at the recorded level


def test_a_stop_is_never_loosened_or_left_unchanged_or_placed_above_the_price(monkeypatch, db):
    client, settings, aid = live_trade(monkeypatch)
    t = the_trade(aid)
    price = t.entry_price * 1.05
    client.price = price
    assert run_move(aid, client, settings, new_stop=t.stop_loss_price - 1.0, price=price)[0] == ["skipped"]  # lower: a loosening
    assert run_move(aid, client, settings, new_stop=t.stop_loss_price, price=price)[0] == ["skipped"]  # the same level
    assert run_move(aid, client, settings, new_stop=price + 1.0, price=price)[0] == ["skipped"]  # beyond the price: it would trigger at once
    assert the_trade(aid).stop_loss_price == t.stop_loss_price and len(resting_stops(client)) == 1


def test_when_binance_refuses_a_second_close_position_stop_the_existing_reduce_only_fallback_still_places_the_new_one_first(monkeypatch, db):
    client, settings, aid = live_trade(monkeypatch)
    t = the_trade(aid)
    price = t.entry_price * 1.05
    client.price = price
    client.reject_second_close_stop = True  # models Binance refusing a second closePosition stop while one rests (UNVERIFIED); the venue then places a reduceOnly stop instead
    calls_before = len(client.calls)
    results, n = run_move(aid, client, settings, new_stop=t.entry_price * 1.01, price=price)
    assert results == ["moved"]
    seq = [c[0] for c in client.calls[calls_before:]]
    assert seq.index("create_order") < seq.index("cancel_order")  # still: new stop first, old one cancelled after
    assert len(resting_stops(client)) == 1 and the_trade(aid).stop_loss_price > t.stop_loss_price


def test_a_refused_new_stop_keeps_the_old_one_and_alerts_once(monkeypatch, db):
    import ccxt

    client, settings, aid = live_trade(monkeypatch)
    t = the_trade(aid)
    price = t.entry_price * 1.05
    client.price = price
    client.fail("create_order", exc=ccxt.InvalidOrder("rejected"), role="stop", times=50)  # every attempt to place the new stop is refused
    old_orders = {k: v["status"] for k, v in client.orders.items()}
    results, n = run_move(aid, client, settings, new_stop=t.entry_price * 1.01, price=price, repeat=2)
    assert results == ["kept", "kept"]
    after = the_trade(aid)
    assert after.stop_loss_price == t.stop_loss_price and after.status == "open" and after.protection_status == "protected" and after.protection_attempt == t.protection_attempt
    assert {k: v["status"] for k, v in client.orders.items() if k in old_orders} == old_orders and len(resting_stops(client)) == 1  # the old stop was never touched
    alerts = [x for x in n.texts if "Could not move the trailing stop" in x]
    assert len(alerts) == 1 and f"{t.stop_loss_price:.4f}" in alerts[0] and "still in place" in alerts[0]  # one alert, however often it is retried


def test_an_unverifiable_new_stop_keeps_the_old_one(monkeypatch, db):
    client, settings, aid = live_trade(monkeypatch)
    t = the_trade(aid)
    price = t.entry_price * 1.05
    client.price = price

    def unreadable(ctx):
        real = ctx.venue.get_protection

        async def get_protection(*, kind, client_id):
            if client_id.endswith("2") or "stop-2" in client_id or client_id != lifecycle.client_id(t, "stop", t.protection_attempt):
                return None  # the new order cannot be read back
            return await real(kind=kind, client_id=client_id)

        ctx.venue.get_protection = get_protection

    results, _ = run_move(aid, client, settings, new_stop=t.entry_price * 1.01, price=price, prepare=unreadable)
    assert results == ["kept"] and the_trade(aid).stop_loss_price == t.stop_loss_price
    assert len(resting_stops(client)) == 1  # the unverified new order was cancelled, the old one still rests


def test_a_failed_cancel_of_the_old_stop_still_records_the_move(monkeypatch, db):
    client, settings, aid = live_trade(monkeypatch)
    t = the_trade(aid)
    price = t.entry_price * 1.05
    client.price = price

    def no_cancel(ctx):
        async def cancel_protection(**kw):
            raise RuntimeError("cancel failed")

        ctx.venue.cancel_protection = cancel_protection

    results, _ = run_move(aid, client, settings, new_stop=t.entry_price * 1.01, price=price, prepare=no_cancel)
    assert results == ["moved"] and the_trade(aid).stop_loss_price > t.stop_loss_price  # two stops rest; the leftover is cleared when the trade closes


def test_a_spot_move_that_the_exchange_refuses_keeps_the_old_stop(monkeypatch, db):
    client, settings, aid = live_trade(monkeypatch, market="spot")
    t = the_trade(aid)
    price = t.entry_price * 1.05
    client.price = price
    results, n = run_move(aid, client, settings, new_stop=t.entry_price * 1.01, price=price)
    # On Spot the first stop locks the held quantity, so a second stop cannot be placed first: the move is refused and the old stop stays.
    assert results == ["kept"] and the_trade(aid).stop_loss_price == t.stop_loss_price and len(resting_stops(client)) == 1
    assert any("still in place" in x for x in n.texts)


def test_a_trade_that_is_not_open_and_protected_is_left_alone(monkeypatch, db):
    client, settings, aid = live_trade(monkeypatch)
    t = the_trade(aid)
    with session_scope() as s:
        s.get(Trade, t.id).protection_status = "pending"
    price = t.entry_price * 1.05
    client.price = price
    assert run_move(aid, client, settings, new_stop=t.entry_price * 1.01, price=price)[0] == ["skipped"]


# ------------------------------------------------------------------ the cycle
def st(**over):
    base_ = dict(donchian_enabled=True, live_donchian=True, strategy_guards_mode="enforce", new_strategy_guards_mode="enforce", fee_guard_margin=0.5, strategy_expectancy_window=0,
                 max_total_risk_pct=5.0, max_notional_multiplier=100.0, trade_telemetry=True, protection_precheck_mode="off")
    base_.update(over)
    return make_settings(**base_)


_n = __import__("itertools").count()


def live_account(settings, *, market="futures", opted=True, **kw):
    kw.setdefault("email", f"ldon{next(_n)}@example.com")
    aid = new_account(settings, market_type=market, **kw)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.extra_strategies = "donchian_breakout_v1" if opted else None
    return aid


def setup(monkeypatch, candles=None, *, market="futures", ema=False, price=103.0, **over):
    settings = st(**over)
    candles = candles if candles is not None else with_last(LONG_ROW)
    mk = futures_client if market == "futures" else spot_client
    client = mk(ohlcv=raw(candles), price=price)
    install(monkeypatch, client)
    if not ema:
        monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    return settings, client


def test_the_switch_is_off_by_default_and_a_live_account_never_runs_the_code_with_it_off(db, monkeypatch):
    assert load_settings().live_donchian is False
    settings, client = setup(monkeypatch, live_donchian=False)

    def boom(*a, **k):
        raise AssertionError("donchian code ran for a live account with the switch off")

    monkeypatch.setattr(D, "signal_from_candles", boom)
    aid = live_account(settings)
    run_cycle(aid, settings)
    assert all_trades(aid) == [] and client.orders == {}


@pytest.mark.parametrize("market", ["futures", "spot"])
def test_an_opted_in_live_account_enters_a_breakout_with_a_verified_protective_stop(db, monkeypatch, market):
    settings, client = setup(monkeypatch, market=market)
    aid = live_account(settings, market=market)
    n = run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.strategy_id == "donchian_breakout_v1" and t.is_paper is False and t.side == "long" and t.status == "open" and t.protection_status == "protected"
    assert t.initial_stop_price == t.stop_loss_price == 100.0  # the channel midline; the trail will move stop_loss_price, never initial_stop_price
    assert any("[LIVE] Entered LONG" in x and "Strategy: donchian_breakout_v1" in x for x in n.texts)


def test_a_not_opted_in_account_and_missing_guards_take_nothing(db, monkeypatch):
    for over, opted in (({}, False), ({"donchian_enabled": False}, True), ({"new_strategy_guards_mode": "observe"}, True), ({"fee_guard_margin": 0.0}, True)):
        settings, client = setup(monkeypatch, **over)
        aid = live_account(settings, opted=opted)
        run_cycle(aid, settings)
        assert all_trades(aid) == [] and client.orders == {}, (over, opted)


def test_donchian_has_priority_over_ema_for_the_one_live_slot(db, monkeypatch):
    settings, client = setup(monkeypatch, ema=True)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: BUY_EMA)
    aid = live_account(settings)
    run_cycle(aid, settings)
    trades = all_trades(aid)
    assert len(trades) == 1 and trades[0].strategy_id == "donchian_breakout_v1"


def test_ema_is_not_even_attempted_once_donchian_took_the_slot(db, monkeypatch):
    settings, client = setup(monkeypatch, ema=True)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: BUY_EMA)
    seen = []
    real = trader._maybe_enter

    async def spy(session, account, client_, signal, *a, **k):
        seen.append(type(signal).__name__)
        return await real(session, account, client_, signal, *a, **k)

    monkeypatch.setattr(trader, "_maybe_enter", spy)
    aid = live_account(settings)
    run_cycle(aid, settings)
    assert seen == ["DonchianSignal"]  # the EMA entry was never tried


def _later(aid, hours):
    with session_scope() as s:
        s.query(Trade).filter(Trade.account_id == aid).one().opened_at = datetime.now(timezone.utc) - timedelta(hours=hours)


def test_an_open_donchian_trade_trails_its_stop_up_and_never_changes_the_initial_stop(db, monkeypatch):
    settings, client = setup(monkeypatch)
    aid = live_account(settings)
    run_cycle(aid, settings)
    first = the_trade(aid)
    cs = rising(4)
    client._ohlcv, client.price = raw(cs), cs[-1].close + 0.5
    _later(aid, 5.5)  # the four new candles all started after the entry
    n = run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "open" and t.protection_status == "protected" and t.stop_loss_price > first.stop_loss_price and t.initial_stop_price == first.initial_stop_price
    assert t.stop_loss_price < client.price and len(resting_stops(client)) == 1 and t.protection_attempt == first.protection_attempt + 1
    # a second cycle with the same candles changes nothing (the level has not improved)
    before = (t.stop_loss_price, t.protection_attempt)
    run_cycle(aid, settings)
    assert (the_trade(aid).stop_loss_price, the_trade(aid).protection_attempt) == before


def test_the_trail_keeps_following_new_highs_on_later_cycles(db, monkeypatch):
    settings, client = setup(monkeypatch)
    aid = live_account(settings)
    run_cycle(aid, settings)
    cs = rising(3)
    client._ohlcv, client.price = raw(cs), cs[-1].close + 0.5
    _later(aid, 4.5)
    run_cycle(aid, settings)
    first_trail = the_trade(aid).stop_loss_price
    cs2 = rising(6)  # three more higher candles
    client._ohlcv, client.price = raw(cs2), cs2[-1].close + 0.5
    _later(aid, 7.5)
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.stop_loss_price > first_trail and t.initial_stop_price == 100.0 and t.status == "open"  # judged from the INITIAL stop, so the trail keeps moving


def test_a_close_beyond_the_opposite_channel_exits_through_the_lifecycle(db, monkeypatch):
    settings, client = setup(monkeypatch)
    aid = live_account(settings)
    run_cycle(aid, settings)
    cs = with_last(LONG_ROW)
    # drift up, then a candle that CLOSES below the lowest low of the previous ten, without touching the stops (the stop is far below)
    for k in range(11):
        cs.append(cd(41 + k, 103.0, 103.6, 102.6, 103.2, 150.0))
    cs.append(cd(52, 103.2, 103.3, 101.0, 101.1, 150.0))
    client._ohlcv, client.price = raw(cs), 101.1
    _later(aid, 12.5)
    n = run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "channel_exit" and t.is_paper is False
    assert any("[LIVE] Exited" in x and "channel exit" in x for x in n.texts)


def test_an_ema_crossover_does_not_close_a_donchian_trade(db, monkeypatch):
    settings, client = setup(monkeypatch)
    aid = live_account(settings)
    run_cycle(aid, settings)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: StrategySignal(SignalType.BEARISH_CROSSOVER, "bearish crossover", 100.0, 101.0, 1.0, 0.01))
    run_cycle(aid, settings)
    assert the_trade(aid).status == "open"


def test_an_open_donchian_trade_is_still_managed_with_the_switch_off(db, monkeypatch):
    settings, client = setup(monkeypatch)
    aid = live_account(settings)
    run_cycle(aid, settings)
    first = the_trade(aid)
    cs = rising(4)
    client._ohlcv, client.price = raw(cs), cs[-1].close + 0.5
    _later(aid, 5.5)
    run_cycle(aid, dataclasses.replace(settings, live_donchian=False))
    assert the_trade(aid).stop_loss_price > first.stop_loss_price  # a trade that exists keeps being managed with its own rules


def test_the_entry_function_refuses_a_live_donchian_call_with_the_switch_off(db, monkeypatch):
    settings, client = setup(monkeypatch, live_donchian=False)
    aid = live_account(settings)
    sig = D.DonchianSignal(side="long", reason="x", close=103.0, stop=100.0, atr=2.0)

    async def go():
        with session_scope(autoflush=False) as session:
            await trader._maybe_enter(session, session.get(TradingAccount, aid), client, sig, 103.0, settings, FakeNotifier(), is_paper=False, now=datetime.now(timezone.utc))

    asyncio.run(go())
    assert all_trades(aid) == [] and client.orders == {}


def test_the_trail_goes_through_move_stop_only_and_exits_through_execute_exit():
    import inspect

    src = inspect.getsource(trader._live_donchian_rules)
    assert "lifecycle.move_stop" in src and "lifecycle.execute_exit" in src and "create_order" not in src and "cancel_order" not in src


# ------------------------------------------------------------------ the migration
def test_the_migration_is_additive_idempotent_and_reversible(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "a6e2c4f8b1d3")
    cols = lambda: {c["name"] for c in inspect(create_engine(f"sqlite:///{path}")).get_columns("trades")}
    before = cols()
    command.upgrade(cfg, "b7f3d5a9c2e4")
    assert cols() == before | {"initial_stop_price"}
    command.upgrade(cfg, "b7f3d5a9c2e4")
    command.downgrade(cfg, "a6e2c4f8b1d3")
    assert cols() == before
    command.upgrade(cfg, "head")
    assert "initial_stop_price" in cols()


def test_the_pinned_strategy_file_is_untouched():
    """donchian.py is pinned by the pre-registered backtest plan hash (tests/test_audit_m3_plan_hash.py); the trailing code lives in its own module."""
    assert not hasattr(D, "trailing_level") and hasattr(TR, "trailing_level")
