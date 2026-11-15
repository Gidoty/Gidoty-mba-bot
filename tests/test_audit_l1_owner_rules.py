"""Audit L1: a paper rsi_mr_v1 or donchian_breakout_v1 trade on an account that now resolves as LIVE is still managed by the rules of the strategy that opened it
(its own stop, target, trail, channel), on the PAPER environment, and nothing new is entered. Chosen over "refuse" because refusing would leave the trade with no exit at all.
No order is placed: paper trades are database rows."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from helpers_live import all_trades, install, make_settings, new_account, run_cycle, spot_client
from test_donchian import base, cd

from mba_bot import exchange, trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade, TradingAccount
from mba_bot.strategy import SignalType, StrategySignal

U = timezone.utc
HOUR = 3_600_000
KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")
NO_EMA = StrategySignal(SignalType.NONE, "no crossover", 100.0, 100.0, 1.0, 0.01)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def setup(monkeypatch, rows_after, *, strategy, entry, stop, **over):
    """A LIVE-mode account holding one open PAPER trade of `strategy`; the candles after the entry are `rows_after`."""
    st = make_settings(**dict(KEYS, rsi_mr_enabled=True, donchian_enabled=True, new_strategy_guards_mode="enforce", fee_guard_margin=0.5,
                              max_notional_multiplier=100.0, **over))
    pre = [104.0 - 0.1 * i for i in range(40)] if strategy == "rsi_mr_v1" else [c for c in base(40)]
    cs = []
    end = int(time.time() * 1000) - 60_000
    n = len(pre) + len(rows_after)
    for i, c in enumerate(pre):
        v = c if isinstance(c, float) else None
        cs.append([end - (n - i) * HOUR, (v if v is not None else c.open), (v + 0.3 if v is not None else c.high), (v - 0.3 if v is not None else c.low),
                   (v if v is not None else c.close), 1.0 if v is not None else c.volume])
    for j, r in enumerate(rows_after):
        cs.append([end - (len(rows_after) - j) * HOUR, *r, 100.0])
    client = spot_client(ohlcv_by_pair={p: cs for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=cs)
    install(monkeypatch, client)
    seen = []
    real_build = exchange.build_exchange_client

    def spy(**kw):
        seen.append(kw.get("sandbox"))
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", spy)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    aid = new_account(st)  # LIVE mode
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.extra_strategies = "rsi_mr_v1,donchian_breakout_v1"
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=entry, quantity=1.0, stop_loss_price=stop, status="open", pair="BTC/USDT", strategy_id=strategy,
                    opened_at=datetime.fromtimestamp((end - len(rows_after) * HOUR - 5000) / 1000, tz=U)))
    return st, client, aid, seen


def test_an_rsi_paper_trade_on_a_live_mode_account_exits_by_its_own_target(db, monkeypatch):
    st, client, aid, seen = setup(monkeypatch, [(100.0, 102.4, 99.8, 102.2)], strategy="rsi_mr_v1", entry=100.0, stop=97.0)
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert t.status == "closed" and t.close_reason == "profit_target" and t.is_paper
    assert seen and all(s is True for s in seen)  # the paper environment, never the live one
    assert not [c for c in client.calls if c[0] == "create_order"]


def test_a_donchian_paper_trade_on_a_live_mode_account_exits_by_its_own_stop(db, monkeypatch):
    st, client, aid, seen = setup(monkeypatch, [(103.0, 103.5, 99.0, 100.5)], strategy="donchian_breakout_v1", entry=103.0, stop=100.0)
    run_cycle(aid, st)
    t = all_trades(aid)[0]
    assert t.status == "closed" and t.close_reason == "stop_loss" and t.exit_price == 100.0
    assert all(s is True for s in seen) and not [c for c in client.calls if c[0] == "create_order"]


def test_nothing_new_is_entered_by_a_live_mode_account_in_that_cycle(db, monkeypatch):
    st, client, aid, seen = setup(monkeypatch, [(100.0, 100.1, 99.9, 100.0)] * 3, strategy="rsi_mr_v1", entry=100.0, stop=97.0)
    run_cycle(aid, st)
    assert len(all_trades(aid)) == 1 and all_trades(aid)[0].status == "open"


def test_an_ema_paper_trade_on_a_live_mode_account_is_still_on_the_single_trade_path(db, monkeypatch):
    st = make_settings(**dict(KEYS))
    aid = new_account(st)
    with session_scope() as s:
        s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=100.0, quantity=1.0, stop_loss_price=90.0, status="open", pair="BTC/USDT",
                    strategy_id="ema_cross_atr_v1", opened_at=datetime.now(U)))
        acct = s.get(TradingAccount, aid)
        open_trades = s.query(Trade).filter(Trade.account_id == aid).all()
        assert trader._use_multi(acct, st, False, open_trades) is False
