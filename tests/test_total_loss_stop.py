"""B1c: the total loss stop (ACCOUNT_TOTAL_LOSS_STOP) and the proof that the daily pause does NOT scale with risk per trade.

PROVES (fake exchange, scratch SQLite, frozen clock): off by default (no read, no write); the reference is the first healthy reading; the stop acts at max(10, 3 x risk) percent (cap 80) and
not before; new entries stop, an open trade is untouched, nothing resets by itself (not even the next day), the customer's reset releases it and takes a fresh reference; an unreadable equity blocks
(fail closed); a deposit or withdrawal is handled only through the reset; the daily limit is the same fixed value for accounts at 1, 10, 20 and 40 percent and unchanged when ACCOUNT_DAILY_LOSS_PCT is 0.
DOES NOT PROVE: anything about Binance."""
from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from helpers_live import BEARISH_OHLCV, BULLISH_OHLCV, FLAT_OHLCV, FakeNotifier, all_trades, futures_client, install, make_settings, new_account, open_protected, run_cycle
from sqlalchemy import create_engine, inspect
from test_account_daily_loss import NOON, reasons, rig, rows

from mba_bot import account_loss, exchange, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import AccountDayEquity, AccountLossPause, AccountTotalLossStop, TradingAccount

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def settings_(**over):
    base = dict(max_notional_multiplier=100.0, trade_telemetry=True, account_total_loss_stop=True)
    base.update(over)
    return make_settings(**base)


def fut(monkeypatch, *, risk=1.0, email="live@example.com", **over):
    st = settings_(**over)
    client, clock, aid = rig(monkeypatch, st, market="futures", email=email)
    client.total_margin_balance = 1000.0
    with session_scope() as s:
        s.get(TradingAccount, aid).risk_pct = risk
    return st, client, clock, aid


def stops():
    return rows(AccountTotalLossStop)


def account(aid):
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        return a.loss_reference_equity, a.loss_reference_at


def step(clock, minutes):
    clock.instant = NOON + timedelta(minutes=minutes)


# ------------------------------------------------------------------ off by default
def test_off_by_default_reads_nothing_and_writes_nothing(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0)  # no daily pause, no total stop
    assert st.account_total_loss_stop is False and st.account_daily_loss_pct == 0.0
    client, clock, aid = rig(monkeypatch, st, market="futures")

    async def boom(*a, **k):
        raise AssertionError("no equity read with both switches off")

    monkeypatch.setattr(exchange, "fetch_account_equity_ex", boom)
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st)
    assert account(aid) == (None, None) and stops() == [] and rows(AccountDayEquity) == []


# ------------------------------------------------------------------ reference, threshold, scaling
def test_the_reference_is_the_first_healthy_reading_and_does_not_move(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    run_cycle(aid, st)
    ref, at = account(aid)
    assert ref == 1000.0 and at is not None and stops() == []
    client.total_margin_balance = 1100.0  # equity rises: the reference stays
    step(clock, 5)
    run_cycle(aid, st)
    assert account(aid)[0] == 1000.0


@pytest.mark.parametrize("risk,threshold", [(0.1, 10.0), (1.0, 10.0), (2.0, 10.0), (5.0, 15.0), (10.0, 30.0), (20.0, 60.0), (40.0, 80.0)])
def test_the_stop_acts_at_the_scaled_threshold_and_not_before(db, monkeypatch, risk, threshold):
    st, client, clock, aid = fut(monkeypatch, risk=risk)
    run_cycle(aid, st)
    client.total_margin_balance = 1000.0 * (1 - (threshold - 0.05) / 100)  # a hair under the limit
    step(clock, 5)
    run_cycle(aid, st)
    assert stops() == []
    client.total_margin_balance = 1000.0 * (1 - threshold / 100)  # exactly at the limit
    step(clock, 10)
    n = FakeNotifier()
    run_cycle(aid, st, n)
    assert len(stops()) == 1 and abs(stops()[0].threshold_pct - threshold) < 1e-9 and stops()[0].risk_pct == risk and stops()[0].reset_at is None
    assert len([t for t in n.texts if "total loss limit" in t]) == 1 and "account_total_loss_stop" in reasons()


def test_it_stops_new_entries_and_never_ends_by_itself(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    run_cycle(aid, st)
    client.total_margin_balance = 880.0  # 12% down, limit 10%
    client._ohlcv = BULLISH_OHLCV
    step(clock, 5)
    n = FakeNotifier()
    run_cycle(aid, st, n)
    assert all_trades(aid) == [] and client.orders == {} and len(stops()) == 1
    for minutes in (10, 15):
        step(clock, minutes)
        run_cycle(aid, st, n)
    assert len(stops()) == 1 and len([t for t in n.texts if "total loss limit" in t]) == 1  # one row, one alert
    client.total_margin_balance = 2000.0  # equity is far above the reference again
    for days in (1, 3, 30):
        clock.instant = NOON + timedelta(days=days)
        run_cycle(aid, st, n)
        assert all_trades(aid) == [] and stops()[0].reset_at is None  # no automatic reset, not the next day, not a month later


def test_the_customers_reset_releases_it_and_takes_a_fresh_reference(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    run_cycle(aid, st)
    client.total_margin_balance = 800.0
    client._ohlcv = BULLISH_OHLCV
    step(clock, 5)
    run_cycle(aid, st)
    assert len(stops()) == 1 and all_trades(aid) == []
    with session_scope() as s:
        assert account_loss.reset_total_stop(s, s.get(TradingAccount, aid), NOON + timedelta(minutes=6)) is True
    assert account(aid) == (None, None) and stops()[0].reset_at is not None
    step(clock, 10)
    run_cycle(aid, st)  # new reference from the then-current equity (800), and the entry is allowed
    assert account(aid)[0] == 800.0 and len(stops()) == 1 and len(all_trades(aid)) == 1
    client.total_margin_balance = 800.0 * 0.89  # down 11% from the NEW reference: a new stop
    step(clock, 15)
    run_cycle(aid, st)
    assert len(stops()) == 2 and sum(1 for x in stops() if x.reset_at is None) == 1


def test_a_reset_with_no_stop_active_just_retakes_the_reference(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    run_cycle(aid, st)
    with session_scope() as s:
        assert account_loss.reset_total_stop(s, s.get(TradingAccount, aid), NOON) is False
    client.total_margin_balance = 1500.0  # a deposit
    step(clock, 5)
    run_cycle(aid, st)
    assert account(aid)[0] == 1500.0 and stops() == []


def test_a_deposit_and_a_withdrawal_without_a_reset(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    run_cycle(aid, st)
    client.total_margin_balance = 1500.0  # deposit: the reference stays 1000, a later loss is measured from 1000
    step(clock, 5)
    run_cycle(aid, st)
    client.total_margin_balance = 1000.0 * 0.95
    step(clock, 10)
    run_cycle(aid, st)
    assert stops() == []  # a 5% fall below the reference only: the deposit masked 36% of the account (the documented limitation)
    client.total_margin_balance = 880.0  # a withdrawal looks like a loss and fails safe
    step(clock, 15)
    run_cycle(aid, st)
    assert len(stops()) == 1


def test_an_unreadable_equity_blocks_new_entries_and_records_no_stop(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    run_cycle(aid, st)

    async def broken():
        raise RuntimeError("balance endpoint down")

    client.fetch_balance = broken
    client._ohlcv = BULLISH_OHLCV
    step(clock, 5)
    run_cycle(aid, st, swallow=True)
    assert all_trades(aid) == [] and client.orders == {} and "account_equity_unreadable" in reasons() and stops() == []


def test_an_unreadable_first_reading_sets_no_reference_and_blocks(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)
    client.total_margin_balance = None
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st, swallow=True)
    assert account(aid) == (None, None) and all_trades(aid) == [] and "account_equity_unreadable" in reasons()


def test_the_tick_writes_the_stop_on_an_idle_cycle_without_any_signal(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch)  # a flat market: no signal tries to enter
    run_cycle(aid, st)
    client.total_margin_balance = 850.0
    step(clock, 5)
    run_cycle(aid, st)
    assert len(stops()) == 1 and all_trades(aid) == []


def test_an_open_trade_is_untouched_when_the_stop_acts(db, monkeypatch):
    st = settings_()
    client, clock, aid = rig(monkeypatch, st, at=NOON)  # a live Spot account
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st)
    assert all_trades(aid)[0].status == "open"
    orders = {k: dict(v) for k, v in client.orders.items()}
    client.free["USDT"] -= 300.0  # a large fall in equity with the trade open
    step(clock, 5)
    run_cycle(aid, st)
    assert len(stops()) == 1
    t = all_trades(aid)[0]
    assert t.status == "open" and t.protection_status == "protected" and {k: dict(v) for k, v in client.orders.items()} == orders
    client._ohlcv, client.price = BEARISH_OHLCV, BEARISH_OHLCV[-1][4]
    step(clock, 10)
    run_cycle(aid, st)
    assert all_trades(aid)[0].status == "closed" and all_trades(aid)[0].close_reason == "crossover_exit"  # the exit still runs


def test_paper_accounts_are_not_affected(db, monkeypatch):
    st = settings_(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
    client, clock, aid = rig(monkeypatch, st, balance=1.0)
    with session_scope() as s:
        s.get(TradingAccount, aid).bot_started_at = None  # PAPER

    async def boom(*a, **k):
        raise AssertionError("a paper account reads no equity")

    monkeypatch.setattr(exchange, "fetch_account_equity_ex", boom)
    client._ohlcv = BULLISH_OHLCV
    run_cycle(aid, st)
    assert account(aid) == (None, None) and stops() == []


# ------------------------------------------------------------------ the daily pause does not scale with risk
@pytest.mark.parametrize("risk", [1.0, 10.0, 20.0, 40.0])
def test_the_daily_limit_is_the_same_for_every_risk_setting(db, monkeypatch, risk):
    st = make_settings(max_notional_multiplier=100.0, trade_telemetry=True, account_daily_loss_pct=4.0, risk_pct_max=40.0)  # total stop off
    client, clock, aid = rig(monkeypatch, st, market="futures", email=f"r{int(risk)}@example.com")
    client.total_margin_balance = 1000.0
    with session_scope() as s:
        s.get(TradingAccount, aid).risk_pct = risk
    run_cycle(aid, st)
    client.total_margin_balance = 960.1  # down 3.99%
    step(clock, 5)
    run_cycle(aid, st)
    assert rows(AccountLossPause) == []
    client.total_margin_balance = 960.0  # down exactly 4.0%: the same limit whatever the risk setting
    step(clock, 10)
    run_cycle(aid, st)
    p = rows(AccountLossPause)
    assert len(p) == 1 and p[0].limit_pct == 4.0


def test_the_daily_code_does_not_know_about_risk_per_trade():
    import inspect as ins

    from mba_bot import account_loss as al

    for fn in (al.daily_loss_hit, al.record_sample, al.write_pause, al.active_pause):
        assert "risk" not in "".join(ins.signature(fn).parameters)
    body = ins.getsource(al.daily_loss_hit) + ins.getsource(al.record_sample) + ins.getsource(al.write_pause)
    assert "risk_pct" not in body
    trader_src = ins.getsource(trader._account_loss_check)
    assert "risk_pct" not in trader_src and "risk_limits" not in trader_src


def test_the_daily_pause_is_unchanged_when_it_is_off_even_with_the_total_stop_on(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch, risk=40.0)  # ACCOUNT_DAILY_LOSS_PCT is 0
    assert st.account_daily_loss_pct == 0.0
    run_cycle(aid, st)
    client.total_margin_balance = 700.0  # a 30% fall: far below any daily limit, but the total stop for 40% risk is 80%
    step(clock, 5)
    run_cycle(aid, st)
    assert rows(AccountDayEquity) == [] and rows(AccountLossPause) == [] and stops() == []


def test_both_limits_can_act_in_one_cycle_and_the_daily_state_is_kept(db, monkeypatch):
    st, client, clock, aid = fut(monkeypatch, risk=1.0, account_daily_loss_pct=4.0)
    run_cycle(aid, st)
    client.total_margin_balance = 850.0  # 15% down: past the daily 4% and the total 10%
    step(clock, 5)
    n = FakeNotifier()
    run_cycle(aid, st, n)
    assert len(rows(AccountLossPause)) == 1 and len(stops()) == 1
    assert len([t for t in n.texts if "paused for the rest of today" in t]) == 1 and len([t for t in n.texts if "total loss limit" in t]) == 1


# ------------------------------------------------------------------ migration
def _cfg(monkeypatch, path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def test_the_migration_is_additive_idempotent_and_reversible(tmp_path, monkeypatch, keep_logging):
    path = tmp_path / "m.db"
    cfg = _cfg(monkeypatch, path)
    command.upgrade(cfg, "b9e3d7a2c5f1")
    eng = lambda: inspect(create_engine(f"sqlite:///{path}"))
    cols = lambda: {c["name"] for c in eng().get_columns("trading_accounts")}
    before = cols()
    assert "account_total_loss_stops" not in eng().get_table_names()
    command.upgrade(cfg, "head")
    assert cols() == before | {"loss_reference_equity", "loss_reference_at"} and "account_total_loss_stops" in eng().get_table_names()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "b9e3d7a2c5f1")
    assert cols() == before and "account_total_loss_stops" not in eng().get_table_names()
    command.upgrade(cfg, "head")
    assert "account_total_loss_stops" in eng().get_table_names()


def test_the_head_is_the_new_revision():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["c1f4a8d2e6b3"] and script.get_revision("c1f4a8d2e6b3").down_revision == "b9e3d7a2c5f1"
