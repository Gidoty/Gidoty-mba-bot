"""Part A: the best price reached since entry (docs/safety/profit_peak_tracking.md).

PROVES: the peak only ever moves in the favourable direction (long and short), a candle that started before the entry is never counted, the forming candle and the live price are, an old open trade with no peak gets
one at its first managed round, live and paper trades both get it, the admin panel shows it, a failed write never stops trade management, and nothing in the entry path reads it.
DOES NOT PROVE: anything about profit. The peak is a measurement."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from helpers_live import run_cycle, the_trade
from helpers_profit import HOUR, candles, live

from mba_bot import admin_service, profit_protect, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade

U = timezone.utc


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def cd(ts, h, lo):
    return SimpleNamespace(timestamp=ts, high=h, low=lo, open=h, close=lo)


# ---------------------------------------------------------------------------------------------- pure
def test_the_peak_only_moves_in_the_favourable_direction_for_a_long_and_a_short():
    assert profit_protect.advance_peak("long", 100.0, 105.0) == 105.0
    assert profit_protect.advance_peak("long", 105.0, 101.0) == 105.0  # a lower price never lowers a long's peak
    assert profit_protect.advance_peak("short", 100.0, 95.0) == 95.0
    assert profit_protect.advance_peak("short", 95.0, 99.0) == 95.0  # a higher price never raises a short's peak
    assert profit_protect.advance_peak("long", None, 90.0) == 90.0
    for bad in (None, 0.0, -1.0, float("nan")):
        assert profit_protect.advance_peak("long", 100.0, bad) == 100.0
        assert profit_protect.advance_peak("short", 100.0, bad) == 100.0
    assert profit_protect.is_better("long", 2.0, 1.0) and not profit_protect.is_better("long", 1.0, 1.0)
    assert profit_protect.is_better("short", 1.0, 2.0) and not profit_protect.is_better("short", 2.0, 2.0)


def test_a_candle_from_before_the_entry_is_never_counted_but_the_forming_candle_and_the_price_are():
    entry, opened = 100.0, 1000
    before = cd(500, 500.0, 1.0)  # started before the entry: its extreme may predate the trade
    after = cd(1000, 120.0, 99.0)
    assert profit_protect.peak_from_candles("long", entry, opened, [before, after], None, None) == 120.0
    assert profit_protect.peak_from_candles("long", entry, opened, [before], None, None) == 100.0  # nothing after the entry: the entry price
    assert profit_protect.peak_from_candles("long", entry, opened, [before, after], 130.0, None) == 130.0  # the live price counts
    assert profit_protect.peak_from_candles("long", entry, opened, [after], 105.0, 125.0) == 125.0  # a stored peak is never lowered
    assert profit_protect.peak_from_candles("short", entry, opened, [cd(1000, 101.0, 80.0), before], None, None) == 80.0
    assert profit_protect.peak_from_candles("short", entry, opened, [cd(1000, 101.0, 99.0)], 120.0, None) == 99.0  # a worse price does nothing for a short
    assert profit_protect.peak_from_candles("long", entry, opened, [cd(1000, 99.0, 90.0)], 95.0, None) == 100.0  # never worse than the entry


# ---------------------------------------------------------------------------------------------- live
def test_a_live_trade_records_the_peak_and_a_pullback_never_lowers_it(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch)
    assert the_trade(aid).peak_price is None  # opened before any round managed it
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 120)])
    run_cycle(aid, settings)
    assert the_trade(aid).peak_price == 140.0
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 120), (120, 125, 110, 112)])
    run_cycle(aid, settings)
    assert the_trade(aid).peak_price == 140.0  # lower highs and a lower price: unchanged
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 120), (120, 150, 110, 112)])
    run_cycle(aid, settings)
    assert the_trade(aid).peak_price == 150.0


def test_an_old_open_trade_with_no_peak_gets_one_at_its_first_round_from_what_can_be_seen(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch)
    with session_scope() as s:
        assert s.get(Trade, the_trade(aid).id).peak_price is None
    candles(client, base, [(95, 100, 92, 99)])
    run_cycle(aid, settings)
    assert the_trade(aid).peak_price == 100.0  # a lower bound: the highest high the candles show after the entry, never below the entry price 90


def test_the_peak_is_written_even_with_every_profit_rule_off_and_changes_no_order(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch)
    candles(client, base, [(95, 100, 92, 99), (99, 140, 98, 139)])
    before = [x[0] for x in client.calls if x[0] == "create_order"]
    run_cycle(aid, settings)
    t = the_trade(aid)
    assert t.peak_price == 140.0 and t.stop_loss_price == 43.0 and t.status == "open"
    assert [x[0] for x in client.calls if x[0] == "create_order"] == before


def test_a_failed_peak_write_never_stops_trade_management(db, monkeypatch):
    from helpers_live import BEAR_PRICE, BEARISH_OHLCV, open_protected

    client, settings, aid = open_protected(monkeypatch, market="futures")  # a real entry; then the market turns and the strategy's own exit is due

    def boom(*a, **k):
        raise RuntimeError("disk")

    monkeypatch.setattr(profit_protect, "peak_from_candles", boom)
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    run_cycle(aid, settings)
    assert the_trade(aid).status == "closed" and the_trade(aid).close_reason == "crossover_exit"  # the exit still happened


def test_a_trade_that_is_not_open_gets_no_peak_and_a_bad_trade_does_not_raise():
    t = SimpleNamespace(status="closed", entry_price=100.0, opened_at=datetime.now(U), side="long", peak_price=None, id="x")
    trader._track_peak(SimpleNamespace(commit=lambda: None, rollback=lambda: None), t, [], 150.0, now_ms=0)
    assert t.peak_price is None
    trader._track_peak(SimpleNamespace(commit=lambda: None, rollback=lambda: None), SimpleNamespace(), [], 150.0, now_ms=0)  # missing attributes: swallowed


# ---------------------------------------------------------------------------------------------- paper
def test_a_paper_trade_records_the_peak_too(db):
    from helpers_live import make_settings, new_account

    now = datetime.now(U)
    aid = new_account(make_settings(), market_type="futures", email="paper-peak@x.com")
    with session_scope() as s:
        t = Trade(account_id=aid, side="long", is_paper=True, entry_price=100.0, quantity=1.0, stop_loss_price=90.0, initial_stop_price=90.0, status="open", opened_at=now - timedelta(hours=3), pair="BTC/USDT")
        s.add(t)
        s.flush()
        trader._track_peak(s, t, [cd(int((now - timedelta(hours=2)).timestamp() * 1000), 130.0, 100.0)], 120.0, now_ms=int(now.timestamp() * 1000))
        assert t.peak_price == 130.0
        trader._track_peak(s, t, [cd(int((now - timedelta(hours=1)).timestamp() * 1000), 110.0, 100.0)], 105.0, now_ms=int(now.timestamp() * 1000))
        assert t.peak_price == 130.0


# ---------------------------------------------------------------------------------------------- admin and entry path
def test_the_admin_panel_shows_the_peak_and_what_it_was_in_r(db, monkeypatch):
    client, settings, aid, base = live(monkeypatch)
    candles(client, base, [(95, 100, 92, 99), (99, 137, 98, 120)])
    run_cycle(aid, settings)
    with session_scope() as s:
        panel = admin_service.open_trades_panel(s, datetime.now(U))
    row = panel["trades"][0]
    assert row["peak_price"] == 137.0 and row["entry_price"] == 90.0 and row["initial_stop_price"] == 43.0 and row["peak_r"] == 1.0
    assert set(row) >= {"bot_code", "pair", "strategy", "side", "stop_loss_price"} and "email" not in row and "customer_id" not in row
    assert admin_service.r_reached("long", 90.0, 43.0, 90.0) == 0.0 and admin_service.r_reached("short", 100.0, 110.0, 80.0) == 2.0
    assert admin_service.r_reached("long", 90.0, None, 100.0) is None and admin_service.r_reached("long", 90.0, 90.0, 100.0) is None


def test_nothing_in_the_entry_path_reads_the_peak():
    for fn in (trader._maybe_enter, trader._ema_entry_stale, trader._live_strategy_signal, trader._live_rsi_entry, trader._live_donchian_entry):
        assert "peak_price" not in inspect.getsource(fn), fn.__name__


# ---------------------------------------------------------------------------------------------- migration
def test_the_migration_adds_one_nullable_column_is_idempotent_and_reversible(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("d4a1b7c3e9f2_*.py"))
    spec = importlib.util.spec_from_file_location("peak_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "c3f8a2d5b7e9"
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    with eng.begin() as c:
        c.execute(text("create table trades (id varchar(36) primary key, entry_price float)"))
        c.execute(text("insert into trades values ('t1', 100.0)"))

    def run(fn):
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    run(mig.upgrade)
    run(mig.upgrade)  # idempotent
    cols = {c["name"]: c for c in inspect(eng).get_columns("trades")}
    assert cols["peak_price"]["nullable"] is True
    with eng.begin() as c:
        assert c.execute(text("select peak_price from trades")).scalar() is None  # an old row stays NULL: no backfill
    run(mig.downgrade)
    run(mig.downgrade)
    assert "peak_price" not in {c["name"] for c in inspect(eng).get_columns("trades")}
