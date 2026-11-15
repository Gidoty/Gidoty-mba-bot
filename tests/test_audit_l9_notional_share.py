"""Audit L9 (owner decision): when more than one trade slot is configured (MAX_OPEN_POSITIONS above 1), one PAPER trade may use at most 1/slots of the balance as
notional (one third for three slots, a half for two), the first trade included. With one slot nothing changes."""
from __future__ import annotations

import time

import pytest
from helpers_live import all_trades, install, make_settings, new_account, run_cycle, spot_client
from test_donchian import with_last

from mba_bot import multi_trade as mt
from mba_bot import trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import TradingAccount
from mba_bot.risk import PositionSize
from mba_bot.strategy import SignalType, StrategySignal

HOUR = 3_600_000
KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
G3 = mt.Grade(3, ())


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def acct(st):
    aid = new_account(st)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None
        a.extra_strategies = "donchian_breakout_v1"
        a.risk_pct = 2.0
    return aid


@pytest.mark.parametrize("slots,share", [(1, None), (2, 0.5), (3, 1 / 3)])
def test_the_limit_function_trims_a_large_first_trade(db, slots, share):
    st = make_settings(**dict(KEYS, max_open_positions=slots))
    aid = acct(st)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        size = PositionSize(quantity=100.0, risk_amount=100.0, stop_distance=1.0)  # notional 10000 = the whole paper balance
        out, refusal = trader._multi_paper_limits(s, a, st, size, 100.0, trader.MultiInfo(cap=slots, n_open=0, grade=G3))
    assert refusal is None
    notional = out.quantity * 100.0
    if share is None:
        assert out.quantity == 100.0  # one slot: unchanged
    else:
        assert notional == pytest.approx(10_000.0 * share)
        assert out.risk_amount == pytest.approx(out.quantity * 1.0)  # the money at risk follows the smaller size


def test_a_small_trade_is_not_touched(db):
    st = make_settings(**dict(KEYS, max_open_positions=3))
    aid = acct(st)
    with session_scope() as s:
        out, _ = trader._multi_paper_limits(s, s.get(TradingAccount, aid), st, PositionSize(quantity=10.0, risk_amount=10.0, stop_distance=1.0), 100.0,
                                            trader.MultiInfo(cap=3, n_open=0, grade=G3))
    assert out.quantity == 10.0


def run_don(monkeypatch, slots):
    st = make_settings(**dict(KEYS, donchian_enabled=True, new_strategy_guards_mode="enforce", fee_guard_margin=0.5, donchian_risk_pct=2.0, max_open_positions=slots,
                              max_total_risk_pct=10.0, max_notional_multiplier=100.0))
    cs = with_last((100.0, 103.5, 100.5, 103.0, 200.0))
    end = int(time.time() * 1000) - 60_000
    data = [[end - (len(cs) - i) * HOUR, c.open, c.high, c.low, c.close, c.volume] for i, c in enumerate(cs)]
    client = spot_client(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: StrategySignal(SignalType.NONE, "x", 1.0, 1.0, 1.0, 0.01))
    monkeypatch.setattr(trader, "_market_grade", lambda *a, **k: G3)
    aid = acct(st)
    run_cycle(aid, st)
    return all_trades(aid)


def test_in_the_cycle_a_three_slot_account_caps_the_notional_at_one_third(db, monkeypatch):
    trades = run_don(monkeypatch, 3)
    assert len(trades) == 1
    t = trades[0]
    assert t.quantity * t.entry_price == pytest.approx(10_000.0 / 3)  # 2% risk would have been about 6867


def test_in_the_cycle_a_one_slot_account_is_unchanged(db, monkeypatch):
    trades = run_don(monkeypatch, 1)
    t = trades[0]
    assert t.quantity * (t.entry_price - t.stop_loss_price) == pytest.approx(200.0)  # the full 2 percent risk, no notional cap
