"""Audit H2 and M1: rsi_mr_v1 and donchian_breakout_v1 are opted in PER ACCOUNT (trading_accounts.extra_strategies, set by the owner in the database; NULL means
EMA only) and read their own guard mode NEW_STRATEGY_GUARDS_MODE (default off), in which only `enforce` lets them trade. STRATEGY_GUARDS_MODE then affects only EMA."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest
from helpers_live import FakeNotifier, all_trades, install, make_settings, new_account, run_cycle, spot_client
from test_donchian import with_last
from test_rsi_mr import LONG, mk

from mba_bot import multi_trade as mt
from mba_bot import trader
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.models import TradingAccount
from mba_bot.strategy import SignalType, StrategySignal

U = timezone.utc
HOUR = 3_600_000
KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
NO_EMA = StrategySignal(SignalType.NONE, "no crossover", 100.0, 100.0, 1.0, 0.01)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def raw(candles):
    end = int(time.time() * 1000) - 60_000
    n = len(candles)
    return [[end - (n - i) * HOUR, c.open, c.high, c.low, c.close, c.volume] for i, c in enumerate(candles)]


def settings(**over):
    base = dict(KEYS, rsi_mr_enabled=True, donchian_enabled=True, strategy_guards_mode="off", new_strategy_guards_mode="enforce", fee_guard_margin=0.5,
                strategy_expectancy_window=0, max_total_risk_pct=5.0, max_notional_multiplier=100.0)
    base.update(over)
    return make_settings(**base)


def paper(st, email="x@example.com", extra=None, selection="manual"):
    aid = new_account(st, email=email)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None  # PAPER
        a.extra_strategies = extra
        a.pair_selection_mode = selection
    return aid


def rig(monkeypatch, candles):
    data = raw(candles)
    client = spot_client(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    return client


DON = with_last((100.0, 103.5, 100.5, 103.0, 200.0))
RSI = mk(LONG)


# ---------------------------------------------------------------- the per-account opt-in
def test_the_column_is_parsed_safely():
    A = type("A", (), {})
    a = A()
    assert mt.account_strategies(a) == frozenset()
    for raw_value, want in [(None, set()), ("", set()), ("rsi_mr_v1", {"rsi_mr_v1"}), (" RSI_MR_V1 , donchian_breakout_v1 ", {"rsi_mr_v1", "donchian_breakout_v1"}),
                            ("ema_cross_atr_v1,nonsense", set()), ("rsi_mr_v1;donchian_breakout_v1", set())]:
        a.extra_strategies = raw_value
        assert mt.account_strategies(a) == want, raw_value


def test_an_account_that_is_not_opted_in_never_trades_the_new_strategies_and_keeps_the_ema_path(db, monkeypatch):
    st = settings()
    rig(monkeypatch, DON)
    aid = paper(st, extra=None)

    async def no_multi(*a, **k):
        raise AssertionError("the strategy-aware path ran for an account that is not opted in")

    monkeypatch.setattr(trader, "_process_multi", no_multi)
    run_cycle(aid, st)
    run_cycle(aid, st)
    assert all_trades(aid) == []


def test_an_account_that_is_not_opted_in_keeps_auto_select(db, monkeypatch):
    st = settings()
    rig(monkeypatch, DON)
    calls = []

    async def scan(*a, **k):
        calls.append(1)
        return None

    monkeypatch.setattr(trader, "_scan_auto_pair", scan)
    aid = paper(st, extra=None, selection="auto")
    run_cycle(aid, st)
    assert calls  # the Auto Select scan still runs for this paper account


def test_an_opted_in_account_trades_and_one_that_opted_in_to_the_other_strategy_does_not(db, monkeypatch):
    st = settings()
    rig(monkeypatch, DON)
    donchian = paper(st, "d@example.com", extra="donchian_breakout_v1")
    rsi_only = paper(st, "r@example.com", extra="rsi_mr_v1")
    run_cycle(donchian, st)
    run_cycle(rsi_only, st)
    assert [t.strategy_id for t in all_trades(donchian)] == ["donchian_breakout_v1"]
    assert all_trades(rsi_only) == []


def test_the_entry_itself_refuses_an_account_that_is_not_opted_in(db, monkeypatch):
    from mba_bot import donchian as D
    st = settings()
    client = rig(monkeypatch, DON)
    aid = paper(st, extra=None)
    sig = D.signal_from_candles(DON)
    assert sig.side == "long"

    async def go():
        with session_scope(autoflush=False) as s:
            await trader._maybe_enter(s, s.get(TradingAccount, aid), client, sig, 103.0, st, FakeNotifier(), is_paper=True, now=datetime.now(U))

    asyncio.run(go())
    assert all_trades(aid) == []


def test_a_live_account_ignores_the_opt_in_column(db, monkeypatch):
    st = settings()
    rig(monkeypatch, DON)
    aid = new_account(st)  # LIVE
    with session_scope() as s:
        s.get(TradingAccount, aid).extra_strategies = "rsi_mr_v1,donchian_breakout_v1"
    run_cycle(aid, st, swallow=True)
    assert not [t for t in all_trades(aid) if t.strategy_id in ("rsi_mr_v1", "donchian_breakout_v1")]


# ---------------------------------------------------------------- the separate guard mode (M1)
def test_new_strategy_guards_mode_defaults_off_and_parses_like_the_other_modes(monkeypatch):
    assert load_settings().new_strategy_guards_mode == "off"
    for raw_value, want in [("enforce", "enforce"), ("observe", "observe"), (" ENFORCE ", "enforce"), ("yes", "off"), ("", "off")]:
        monkeypatch.setenv("NEW_STRATEGY_GUARDS_MODE", raw_value)
        assert load_settings().new_strategy_guards_mode == want


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_the_new_strategies_trade_only_when_their_own_mode_is_enforce(db, monkeypatch, mode):
    st = settings(new_strategy_guards_mode=mode, strategy_guards_mode="enforce")  # the EMA mode being enforce changes nothing for them
    rig(monkeypatch, DON)
    aid = paper(st, extra="donchian_breakout_v1")
    run_cycle(aid, st)
    assert all_trades(aid) == []


def test_observe_mode_no_longer_lets_a_guard_hit_through(db, monkeypatch):
    st = settings(new_strategy_guards_mode="observe", fee_guard_margin=1000.0)  # the audit's reproduction: an always-hit fee guard in observe mode
    rig(monkeypatch, RSI)
    aid = paper(st, extra="rsi_mr_v1")
    run_cycle(aid, st)
    assert all_trades(aid) == []


def test_the_existing_mode_does_not_affect_the_new_strategies_and_theirs_does_not_affect_ema(db, monkeypatch):
    st = settings(strategy_guards_mode="off", new_strategy_guards_mode="enforce")
    rig(monkeypatch, DON)
    aid = paper(st, extra="donchian_breakout_v1")
    run_cycle(aid, st)
    assert [t.strategy_id for t in all_trades(aid)] == ["donchian_breakout_v1"]  # trades with STRATEGY_GUARDS_MODE off
    # an EMA entry on a LIVE account never reaches the guards when STRATEGY_GUARDS_MODE is off, whatever NEW_STRATEGY_GUARDS_MODE says
    st2 = settings(strategy_guards_mode="off", new_strategy_guards_mode="enforce", rsi_mr_enabled=False, donchian_enabled=False, max_notional_multiplier=100.0)
    from mba_bot.strategy import generate_signal as real_generate_signal
    monkeypatch.setattr(trader, "generate_signal", real_generate_signal)  # the real EMA signal on the default bullish candles
    client = spot_client()
    install(monkeypatch, client)

    async def boom(*a, **k):
        raise AssertionError("the guards ran for an EMA entry although STRATEGY_GUARDS_MODE is off")

    monkeypatch.setattr(trader, "_strategy_guards_block", boom)
    live = new_account(st2, email="live@example.com")
    run_cycle(live, st2)
    assert len(all_trades(live)) == 1


def test_the_fee_guard_must_be_on_for_the_new_strategies(db, monkeypatch):
    st = settings(fee_guard_margin=0.0)
    rig(monkeypatch, DON)
    aid = paper(st, extra="donchian_breakout_v1")
    run_cycle(aid, st)
    assert all_trades(aid) == []


def test_the_migration_adds_a_nullable_column_and_is_reversible(tmp_path, monkeypatch, keep_logging):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from sqlalchemy import create_engine, inspect
    root = Path(__file__).resolve().parent.parent
    path = tmp_path / "m.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    cfg = Config(str(root / "alembic.ini"))
    cols = lambda: {c["name"]: c for c in inspect(create_engine(f"sqlite:///{path}")).get_columns("trading_accounts")}
    command.upgrade(cfg, "e7a2c9d4b1f8")
    assert "extra_strategies" not in cols()
    command.upgrade(cfg, "head")
    assert cols()["extra_strategies"]["nullable"] is True
    command.upgrade(cfg, "head")  # idempotent
    command.downgrade(cfg, "e7a2c9d4b1f8")
    assert "extra_strategies" not in cols()


def test_an_account_opted_in_to_one_strategy_never_even_evaluates_the_other(db, monkeypatch):
    """Beyond refusing the entry: the other strategy is not asked for a signal and leaves no decision-log row for this account."""
    from mba_bot import donchian as D
    from mba_bot.models import DecisionLog
    st = settings(trade_telemetry=True)
    rig(monkeypatch, DON)
    asked = []
    real = D.signal_from_candles
    monkeypatch.setattr(D, "signal_from_candles", lambda *a, **k: asked.append(1) or real(*a, **k))
    aid = paper(st, "r@example.com", extra="rsi_mr_v1")
    run_cycle(aid, st)
    assert asked == []
    with session_scope() as s:
        assert not [d for d in s.query(DecisionLog).filter(DecisionLog.account_id == aid).all() if d.strategy_id == "donchian_breakout_v1"]


def test_the_rsi_entry_itself_refuses_an_account_that_is_not_opted_in(db, monkeypatch):
    st = settings()
    client = rig(monkeypatch, RSI)
    aid = paper(st, extra="donchian_breakout_v1")  # opted in to the OTHER strategy only
    from mba_bot import rsi_mr as R
    sig = R.generate_signal(RSI)
    assert sig.side == "long"

    async def go():
        with session_scope(autoflush=False) as s:
            await trader._maybe_enter(s, s.get(TradingAccount, aid), client, sig, 90.0, st, FakeNotifier(), is_paper=True, now=datetime.now(U))

    asyncio.run(go())
    assert all_trades(aid) == []
