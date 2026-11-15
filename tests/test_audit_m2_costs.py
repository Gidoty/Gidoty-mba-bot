"""Audit M2: the statistics behind the loss streak, the expectancy window, the daily loss limit and the market grade charge each strategy's OWN fee and
slippage per market, the same numbers the fee guard uses (EMA: FEE_GUARD_FEE_BPS per side + FEE_GUARD_SLIPPAGE_BPS round trip; rsi_mr_v1 and
donchian_breakout_v1: their per-market fee per side + their slippage per side)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from helpers_live import make_settings, new_account

from mba_bot import strategy_guards as g
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade, TradingAccount

U = timezone.utc


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def trade(strategy, *, entry=100.0, exit_=100.15, qty=1.0, stop=98.0, paper=True):
    return Trade(account_id="a", side="long", is_paper=paper, entry_price=entry, quantity=qty, stop_loss_price=stop, exit_price=exit_, pnl=(exit_ - entry) * qty,
                 status="closed", close_reason="crossover_exit", pair="BTC/USDT", strategy_id=strategy, opened_at=datetime.now(U) - timedelta(hours=3),
                 closed_at=datetime.now(U) - timedelta(minutes=5))


def test_the_cost_model_numbers():
    st = make_settings()
    assert g.cost_model(st, "ema_cross_atr_v1", "spot") == (5.0, 11.0) == g.cost_model(st, None, "futures")  # fee per side, round-trip slippage (the fee guard's own numbers)
    assert g.cost_model(st, "rsi_mr_v1", "spot") == (10.0, 10.0) and g.cost_model(st, "rsi_mr_v1", "futures") == (5.0, 10.0)
    assert g.cost_model(st, "donchian_breakout_v1", "spot") == (10.0, 10.0) and g.cost_model(st, "donchian_breakout_v1", "futures") == (5.0, 10.0)


def test_a_rsi_trade_that_made_0_15_percent_on_spot_is_a_loss_under_its_own_cost_model():
    st = make_settings()
    t = trade("rsi_mr_v1")
    old = g.trade_result(t, 5.0)  # the previous model: 5 bps per side, no slippage
    assert old.net > 0
    new = g.trade_result(t, *g.cost_model(st, "rsi_mr_v1", "spot"))
    assert new.net < 0 and abs(new.net - (0.15 - 0.0015 * (100.0 + 100.15))) < 1e-9  # 10 bps fee + 5 bps slippage per side = 0.30 percent round trip
    assert new.net_r == pytest.approx(new.net / 2.0)


def test_the_per_strategy_model_reaches_the_loss_streak_and_the_daily_loss(db):
    st = make_settings(strategy_expectancy_window=0, rsi_mr_pause_after_losses=2, strategy_daily_loss_pct=0.0)
    aid = new_account(st)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None
        for i in range(2):
            t = trade("rsi_mr_v1")
            t.account_id = aid
            t.closed_at = datetime.now(U) - timedelta(minutes=5 + i)
            s.add(t)
    from mba_bot.rsi_mr import RsiSignal
    sig = RsiSignal("long", "x", atr_pct=0.05, target_atr=1.0)
    with session_scope() as s:
        codes = [h.code for h in g.evaluate(s, s.get(TradingAccount, aid), st, signal=sig, is_paper=True, now=datetime.now(U), strategy_id="rsi_mr_v1", pair="BTC/USDT")]
    assert g.R_LOSS_STREAK in codes  # two trades that "won" 0.15 percent gross are two net losses once the strategy's costs are charged


def test_the_grade_uses_each_trades_own_strategy_costs(db):
    from mba_bot import trader
    from mba_bot.strategy import candles_from_ohlcv
    st = make_settings(grade2_min_trades=10, grade3_min_trades=20)
    aid = new_account(st)
    with session_scope() as s:
        s.get(TradingAccount, aid).bot_started_at = None
        for i in range(10):
            t = trade("rsi_mr_v1")  # +0.15 gross each: a net loss under rsi_mr_v1's model, a small net gain under the old 5 bps model
            t.account_id = aid
            t.closed_at = datetime.now(U) - timedelta(days=2, minutes=i)
            s.add(t)
        s.commit()
        acct = s.get(TradingAccount, aid)
        candles = candles_from_ohlcv([[i, 100.0, 101.0 + (i % 2) * 0.1, 99.0, 100.0 + (0.3 if i % 2 else 0), 1.0] for i in range(40)])
        grade = trader._market_grade(s, acct, st, candles, now=datetime.now(U))
    assert grade.level == 1 and any("net_negative" in r for r in grade.reasons)
