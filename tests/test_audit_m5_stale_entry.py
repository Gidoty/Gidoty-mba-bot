"""Audit M5: a paper RSI or Donchian entry is taken only if the signal candle is the latest closed candle and closed no more than NEW_STRATEGY_ENTRY_WINDOW_MINUTES
(default 10) ago; otherwise it is skipped and logged. The exit check starts AFTER the candle the entry happened in."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import pytest
from helpers_live import all_trades, install, make_settings, new_account, run_cycle, spot_client
from test_donchian import base, cd, with_last
from test_rsi_mr import LONG, mk

from mba_bot import donchian as D
from mba_bot import rsi_mr as R
from mba_bot import trader
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, Trade, TradingAccount
from mba_bot.strategy import SignalType, StrategySignal

U = timezone.utc
HOUR = 3_600_000
KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
NO_EMA = StrategySignal(SignalType.NONE, "no crossover", 100.0, 100.0, 1.0, 0.01)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def rows_closed_ago(candles, minutes):
    """ccxt rows whose last candle closed `minutes` minutes ago."""
    end = int(time.time() * 1000) - int(minutes * 60_000)
    n = len(candles)
    return [[end - (n - i) * HOUR, c.open, c.high, c.low, c.close, c.volume] for i, c in enumerate(candles)]


def run(monkeypatch, candles, minutes, *, strategy, **over):
    st = make_settings(**dict(KEYS, rsi_mr_enabled=True, donchian_enabled=True, new_strategy_guards_mode="enforce", fee_guard_margin=0.5,
                              strategy_expectancy_window=0, max_total_risk_pct=5.0, max_notional_multiplier=100.0, trade_telemetry=True, **over))
    data = rows_closed_ago(candles, minutes)
    client = spot_client(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    aid = new_account(st)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None
        a.extra_strategies = strategy
    run_cycle(aid, st)
    return aid


DON = with_last((100.0, 103.5, 100.5, 103.0, 200.0))
RSI = mk(LONG)
CASES = [(DON, "donchian_breakout_v1"), (RSI, "rsi_mr_v1")]


def test_the_default_window_is_ten_minutes_and_is_clamped(monkeypatch):
    assert load_settings().new_strategy_entry_window_minutes == 10.0
    for raw, want in [("5", 5.0), ("0", 1.0), ("500", 59.0), ("-3", 1.0)]:
        monkeypatch.setenv("NEW_STRATEGY_ENTRY_WINDOW_MINUTES", raw)
        assert load_settings().new_strategy_entry_window_minutes == want


@pytest.mark.parametrize("candles,strategy", CASES, ids=["donchian", "rsi"])
def test_a_fresh_signal_candle_is_taken(db, monkeypatch, candles, strategy):
    aid = run(monkeypatch, candles, 2, strategy=strategy)
    assert [t.strategy_id for t in all_trades(aid)] == [strategy]


@pytest.mark.parametrize("candles,strategy", CASES, ids=["donchian", "rsi"])
def test_a_signal_that_closed_forty_minutes_ago_is_skipped_and_logged(db, monkeypatch, caplog, candles, strategy):
    with caplog.at_level(logging.INFO, logger="mba_bot.trader"):
        aid = run(monkeypatch, candles, 40, strategy=strategy)
    assert all_trades(aid) == []
    assert any("signal_stale" in r.getMessage() and strategy in r.getMessage() for r in caplog.records)
    with session_scope() as s:
        assert any(d.reason_code == "signal_stale" and d.strategy_id == strategy for d in s.query(DecisionLog).all())


@pytest.mark.parametrize("candles,strategy", CASES, ids=["donchian", "rsi"])
def test_the_window_is_configurable(db, monkeypatch, candles, strategy):
    aid = run(monkeypatch, candles, 40, strategy=strategy, new_strategy_entry_window_minutes=45.0)
    assert [t.strategy_id for t in all_trades(aid)] == [strategy]


@pytest.mark.parametrize("candles,strategy", CASES, ids=["donchian", "rsi"])
def test_data_that_lags_by_a_whole_candle_is_not_traded(db, monkeypatch, candles, strategy):
    aid = run(monkeypatch, candles, 70, strategy=strategy, new_strategy_entry_window_minutes=59.0)  # the newest candle in the data closed 70 minutes ago
    assert all_trades(aid) == []


# ---------------------------------------------------------------- the exit check starts after the entry candle
def test_donchian_ignores_the_candle_the_entry_happened_in():
    cs = base(40)
    cs.append(cd(40, 103, 103.5, 99.0, 103.0))  # a wick through the 100 stop in the candle that contains the entry
    entry_inside = 40 * HOUR + 600_000  # the entry was taken 10 minutes into that candle
    assert D.exit_check(side="long", entry=103.0, stop=100.0, opened_ms=entry_inside, closed=cs, tf_ms=HOUR) is None
    assert D.exit_check(side="long", entry=103.0, stop=100.0, opened_ms=40 * HOUR, closed=cs, tf_ms=HOUR).reason == D.EXIT_STOP  # entered at its open: it counts


def test_rsi_ignores_the_candle_the_entry_happened_in():
    pre = [100.0 + 0.0 * i for i in range(30)]
    cs = [cd(i, v, v + 0.1, v - 0.1, v) for i, v in enumerate(pre)]
    cs.append(cd(30, 100, 100.1, 90.0, 99.0))  # a wick through the stop in the entry candle
    assert R.exit_check(side="long", entry=100.0, stop=97.0, opened_ms=30 * HOUR + 600_000, closed=cs, tf_ms=HOUR) is None
    assert R.exit_check(side="long", entry=100.0, stop=97.0, opened_ms=30 * HOUR, closed=cs, tf_ms=HOUR).reason == R.EXIT_STOP


def test_a_pre_entry_wick_does_not_close_a_trade_in_the_cycle(db, monkeypatch):
    st = make_settings(**dict(KEYS, rsi_mr_enabled=True, donchian_enabled=True, new_strategy_guards_mode="enforce", fee_guard_margin=0.5))
    cs = base(40) + [cd(40, 103, 103.5, 99.0, 103.0)]
    end = int(time.time() * 1000) - 60_000
    data = [[end - (len(cs) - i) * HOUR, c.open, c.high, c.low, c.close, c.volume] for i, c in enumerate(cs)]
    client = spot_client(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    aid = new_account(st)
    entry_candle_start = end - HOUR  # the last candle: it started an hour before "end"
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None
        a.extra_strategies = "donchian_breakout_v1"
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=103.0, quantity=1.0, stop_loss_price=100.0, status="open", pair="BTC/USDT",
                    strategy_id="donchian_breakout_v1", opened_at=datetime.fromtimestamp((entry_candle_start + 600_000) / 1000, tz=U)))
    run_cycle(aid, st)
    assert all_trades(aid)[0].status == "open"  # the wick to 99 happened in the candle the trade was entered in: not judged
