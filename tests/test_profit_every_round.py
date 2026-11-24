"""Part B: profit rules every round, with bounded exchange calls (docs/safety/profit_every_round.md).

PROVES: the single decision (hold, move or exit) and each guard in it (never loosened, never the same level, exit when the price is through the level and never delayed by the call limits, the improvement and
interval limits, the first lock exempt from the interval only); the running levels for EMA and Donchian; that with the switch off nothing changes; that with it on a stop follows the peak INSIDE a candle;
that the number of stop moves per hour is bounded; and that every move still goes through lifecycle.move_stop (new stop first, read back, old one cancelled after) and every exit through execute_exit.
DOES NOT PROVE: that any rule makes money, or Binance's rate-limit behaviour (UNVERIFIED)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from helpers_live import BULLISH_OHLCV, run_cycle, the_trade
from helpers_profit import HOUR, live, resting_stops

from mba_bot import config, ema_exits, donchian_trail, profit_protect as pp
from mba_bot.db import init_db, session_scope
from mba_bot.indicators import atr as atr_series
from mba_bot.models import Trade

U = timezone.utc
from helpers_live import make_settings  # noqa: E402

ATR_PERIOD = make_settings().atr_period


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def D(**o):
    base = dict(side="long", level=120.0, current_stop=100.0, price=130.0, entry=100.0, atr=4.0, r=10.0, last_move_ms=None, now_ms=10_000_000, min_interval_s=900, min_improve_atr=0.25)
    base.update(o)
    return pp.decide(**base)


# ---------------------------------------------------------------------------------------------- the decision
def test_no_level_or_a_level_that_is_not_better_holds_and_a_stop_is_never_loosened():
    assert D(level=None).action == "hold" and D(level=0.0).action == "hold"
    assert D(level=100.0).action == "hold" and D(level=99.0).action == "hold"  # the same level or a worse one: never
    assert D(side="short", level=120.0, current_stop=110.0, price=90.0, entry=100.0).action == "hold"  # a short's stop only goes DOWN
    assert D(side="short", level=100.0, current_stop=110.0, price=80.0, entry=100.0).action == "move"


def test_a_price_at_or_through_the_level_exits_and_the_call_limits_never_delay_it():
    assert D(price=120.0).action == "exit" and D(price=119.0).action == "exit"
    assert D(price=120.01).action == "move"
    assert D(side="short", level=90.0, current_stop=110.0, price=90.0, entry=100.0).action == "exit"
    assert D(side="short", level=90.0, current_stop=110.0, price=89.99, entry=100.0).action == "move"
    # a tiny improvement and a move a second ago would both hold, but an exit is not gated
    assert D(level=100.2, price=100.1, last_move_ms=9_999_000).action == "exit"


def test_a_move_must_be_worth_its_calls_and_not_too_soon_and_the_first_lock_is_exempt_from_the_interval():
    assert D(level=100.9).why == "improvement_too_small"  # needs 0.25 x ATR 4 = 1.0
    assert D(level=101.0).action == "move"
    assert D(atr=None, r=10.0, level=100.9).why == "improvement_too_small"  # 0.1 R when no ATR is known
    assert D(atr=None, r=None, level=100.1).action == "move"  # nothing to measure against: the move is allowed
    # the stop already at the entry (locked): the interval applies
    assert D(current_stop=100.0, last_move_ms=10_000_000 - 899_000).why == "too_soon"
    assert D(current_stop=100.0, last_move_ms=10_000_000 - 900_000).action == "move"
    # the stop still below the entry (no lock yet): the first move is not delayed
    assert D(current_stop=90.0, level=105.0, last_move_ms=10_000_000 - 1_000).action == "move"
    assert D(current_stop=90.0, level=90.5, last_move_ms=10_000_000 - 1_000).why == "improvement_too_small"  # ...but still has to be worth it
    assert D(side="short", current_stop=100.0, level=95.0, price=80.0, entry=100.0, last_move_ms=10_000_000 - 1_000).why == "too_soon"


# ---------------------------------------------------------------------------------------------- the running levels
def test_the_ema_running_level_is_breakeven_then_the_trail_and_never_on_the_wrong_side():
    rules = ema_exits.ExitRules(trail_trigger_r=2.0, trail_atr=1.0, breakeven_r=1.0)
    assert ema_exits.running_level("long", 100.0, 90.0, 105.0, rules, 2.0) is None  # up 0.5 R: nothing earned
    assert ema_exits.running_level("long", 100.0, 90.0, 112.0, rules, 2.0) == (100.0, "break_even_stop")
    assert ema_exits.running_level("long", 100.0, 90.0, 125.0, rules, 2.0) == (123.0, "trailing_stop")
    assert ema_exits.running_level("short", 100.0, 110.0, 75.0, rules, 2.0) == (77.0, "trailing_stop")
    assert ema_exits.running_level("long", 100.0, 90.0, 125.0, rules, None) == (100.0, "break_even_stop")  # no ATR: only break-even
    assert ema_exits.running_level("long", 100.0, 100.0, 125.0, rules, 2.0) is None  # R cannot be measured
    assert ema_exits.running_level("long", 100.0, 90.0, 125.0, ema_exits.ExitRules(), 2.0) is None  # every rule off


def test_the_donchian_running_level_is_peak_minus_the_trail_distance_and_mirrors_for_a_short():
    p = donchian_trail.DonchianParams()
    assert donchian_trail.running_level(side="long", peak=110.0, atr=2.0, p=p) == pytest.approx(110.0 - p.trail_atr * 2.0)
    assert donchian_trail.running_level(side="short", peak=90.0, atr=2.0, p=p) == pytest.approx(90.0 + p.trail_atr * 2.0)
    assert donchian_trail.running_level(side="long", peak=None, atr=2.0) is None and donchian_trail.running_level(side="long", peak=110.0, atr=None) is None
    assert donchian_trail.running_level(side="long", peak=110.0, atr=0.0) is None


def test_the_settings_default_off_and_a_typo_never_switches_the_every_round_mode_on(monkeypatch):
    for k in ("PROFIT_EVERY_ROUND", "PROFIT_MOVE_MIN_INTERVAL_SECONDS", "PROFIT_MOVE_MIN_IMPROVE_ATR"):
        monkeypatch.delenv(k, raising=False)
    s = config.load_settings()
    assert (s.profit_every_round, s.profit_move_min_interval_seconds, s.profit_move_min_improve_atr) == (False, 900, 0.25)
    for bad in ("", "no", "tru", "2", "enabled", " "):
        monkeypatch.setenv("PROFIT_EVERY_ROUND", bad)
        assert config.load_settings().profit_every_round is False, bad
    monkeypatch.setenv("PROFIT_EVERY_ROUND", "true")
    monkeypatch.setenv("PROFIT_MOVE_MIN_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("PROFIT_MOVE_MIN_IMPROVE_ATR", "0")
    s = config.load_settings()
    assert (s.profit_every_round, s.profit_move_min_interval_seconds, s.profit_move_min_improve_atr) == (True, 60, 0.01)  # clamped: never an unbounded call rate


# ---------------------------------------------------------------------------------------------- live, EMA
def forming_rows(closed_rows, forming):
    """8 closed hourly rows then the hour still forming (the current hour)."""
    return closed_rows + [forming]


def set_market(client, base, rows, price):
    client._ohlcv = BULLISH_OHLCV + [[base + i * HOUR, o, h, lo, cl, 1.0] for i, (o, h, lo, cl) in enumerate(rows)]
    client.price = price


def closed_atr(client, n_forming=1):
    rows = client._ohlcv[:-n_forming]
    return atr_series([r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows], ATR_PERIOD)[-1]


CLOSED = [(95, 100, 92, 99)] * 7 + [(99, 101, 95, 100)]


def test_with_the_switch_off_a_high_inside_the_forming_candle_moves_nothing(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_trail_trigger_r=1.0, ema_trail_atr=1.0)
    set_market(client, base, forming_rows(CLOSED, (100, 150, 99, 148)), 148.0)
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == 43.0  # the closed candles never reached +1R; the forming one is not looked at


def test_with_the_switch_on_the_stop_follows_the_peak_inside_a_candle_through_move_stop(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_trail_trigger_r=1.0, ema_trail_atr=1.0, profit_every_round=True)
    set_market(client, base, forming_rows(CLOSED, (100, 150, 99, 148)), 148.0)
    run_cycle(aid, settings)
    t = the_trade(aid)
    expected = 150.0 - closed_atr(client)
    assert t.peak_price == 150.0 and t.stop_loss_price == pytest.approx(expected, abs=0.05) and t.stop_loss_price > 43.0
    assert len(resting_stops(client)) == 1 and resting_stops(client)[0]["trigger"] == pytest.approx(t.stop_loss_price) and client.violations == []
    assert t.stop_moved_at is not None and t.initial_stop_price == 43.0 and t.protection_attempt == 2


def test_moves_are_spaced_out_a_second_round_inside_the_interval_places_nothing_and_a_later_one_does(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_trail_trigger_r=1.0, ema_trail_atr=1.0, profit_every_round=True)
    set_market(client, base, forming_rows(CLOSED, (100, 150, 99, 148)), 148.0)
    run_cycle(aid, settings)
    first = the_trade(aid)
    placed = len(client.orders_by_role("stop"))
    set_market(client, base, forming_rows(CLOSED, (100, 175, 99, 173)), 173.0)  # a much higher peak, one round later
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == first.stop_loss_price and len(client.orders_by_role("stop")) == placed  # too soon: no exchange call for it
    assert the_trade(aid).peak_price == 175.0  # the peak is still recorded
    with session_scope() as s:
        s.get(Trade, first.id).stop_moved_at = datetime.now(U) - timedelta(minutes=16)
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price > first.stop_loss_price and len(client.orders_by_role("stop")) == placed + 1 and len(resting_stops(client)) == 1


def test_the_stop_moves_per_hour_are_bounded_by_the_interval_over_twelve_rounds(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_trail_trigger_r=1.0, ema_trail_atr=1.0, profit_every_round=True)
    moves = 0
    last = the_trade(aid).stop_loss_price
    clock = datetime.now(U)
    for k in range(12):  # twelve 5-minute rounds = one hour, the peak climbing every round
        high = 150 + 10 * k
        set_market(client, base, forming_rows(CLOSED, (100, high, 99, high - 2)), high - 2.0)
        run_cycle(aid, settings)
        now_stop = the_trade(aid).stop_loss_price
        moves += now_stop != last
        last = now_stop
        with session_scope() as s:  # age the move stamp by one round (real time does not pass in a test)
            tr = s.get(Trade, the_trade(aid).id)
            if tr.stop_moved_at is not None:
                tr.stop_moved_at = tr.stop_moved_at - timedelta(minutes=5)
    assert moves <= 1 + 3600 // 900  # one unlock move plus one per interval
    assert moves >= 2  # and the rule did keep following the climb
    assert client.violations == [] and len(resting_stops(client)) == 1


def test_a_price_that_falls_back_through_the_earned_level_exits_through_execute_exit(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_trail_trigger_r=1.0, ema_trail_atr=1.0, profit_every_round=True)
    peak = 150.0
    level = peak - 1.0 * closed_atr_for_rows(client, base, CLOSED)
    set_market(client, base, forming_rows(CLOSED, (100, peak, 99, level - 3)), level - 3)  # the peak was reached, then the price fell through the trail
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.status == "closed" and t.close_reason == "trailing_stop" and client.position == 0.0 and len(client.orders_by_role("exit")) == 1


def closed_atr_for_rows(client, base, rows):
    all_rows = BULLISH_OHLCV + [[base + i * HOUR, o, h, lo, cl, 1.0] for i, (o, h, lo, cl) in enumerate(rows)]
    return atr_series([r[2] for r in all_rows], [r[3] for r in all_rows], [r[4] for r in all_rows], ATR_PERIOD)[-1]


def test_the_mode_never_loosens_a_better_stop_that_already_rests(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch, ema_trail_trigger_r=1.0, ema_trail_atr=1.0, profit_every_round=True)
    with session_scope() as s:
        s.get(Trade, the_trade(aid).id).stop_loss_price = 147.0
    set_market(client, base, forming_rows(CLOSED, (100, 150, 99, 149.5)), 149.5)
    before = len(client.orders_by_role("stop"))
    run_cycle(aid, settings)
    assert the_trade(aid).stop_loss_price == 147.0 and len(client.orders_by_role("stop")) == before


# ---------------------------------------------------------------------------------------------- live, Donchian
def test_a_live_donchian_trade_trails_from_the_running_peak_every_round_with_the_switch_on(db, monkeypatch):
    from test_live_donchian import _later, live_account, raw, rising, setup

    settings, client = setup(monkeypatch, profit_every_round=True)
    aid = live_account(settings)
    run_cycle(aid, settings)
    first = the_trade(aid)
    cs = rising(4)
    client._ohlcv, client.price = raw(cs), cs[-1].close + 0.5
    _later(aid, 5.5)
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.peak_price is not None and t.peak_price >= max(c.high for c in cs[-4:])
    assert t.stop_loss_price > first.stop_loss_price and t.stop_loss_price < client.price and t.initial_stop_price == first.initial_stop_price and len(resting_stops(client)) == 1
    again = (t.stop_loss_price, t.protection_attempt)
    run_cycle(aid, settings)  # nothing new: no second move
    assert (the_trade(aid).stop_loss_price, the_trade(aid).protection_attempt) == again


def test_the_stop_moved_at_migration_is_additive_idempotent_and_reversible(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("e5b2c8d4f1a7_*.py"))
    spec = importlib.util.spec_from_file_location("moved_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "d4a1b7c3e9f2"
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    with eng.begin() as c:
        c.execute(text("create table trades (id varchar(36) primary key)"))
        c.execute(text("insert into trades values ('t1')"))

    def run(fn):
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    run(mig.upgrade)
    run(mig.upgrade)
    assert {c["name"]: c for c in inspect(eng).get_columns("trades")}["stop_moved_at"]["nullable"] is True
    with eng.begin() as c:
        assert c.execute(text("select stop_moved_at from trades")).scalar() is None
    run(mig.downgrade)
    run(mig.downgrade)
    assert "stop_moved_at" not in {c["name"] for c in inspect(eng).get_columns("trades")}
