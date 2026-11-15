"""Audit L8: in the multi-trade cycle a failed message must not stop the other open trades from being managed. Exits commit before the message, so the trade is closed
either way; what used to be lost was the REST of the cycle (the other trades' exits)."""
from __future__ import annotations

import time

import pytest
from helpers_live import BEARISH_OHLCV, BULLISH_OHLCV, FakeNotifier, all_trades, install, make_settings, new_account, run_cycle, spot_client
from test_donchian import base, cd

from mba_bot import trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade, TradingAccount
from mba_bot.strategy import SignalType, StrategySignal

HOUR = 3_600_000
KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")


class FlakyNotifier(FakeNotifier):
    """Every send raises, as a Telegram outage would."""

    def __init__(self):
        super().__init__()
        self.attempts = 0

    async def send(self, target, message):
        self.attempts += 1
        raise RuntimeError("telegram is down")


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def test_three_ema_exits_in_one_cycle_all_happen_although_every_message_fails(db, monkeypatch):
    st = make_settings(**dict(KEYS, max_open_positions=3, max_total_risk_pct=5.0))
    client = spot_client(ohlcv_by_pair={p: BEARISH_OHLCV for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=BEARISH_OHLCV)
    install(monkeypatch, client)
    aid = new_account(st)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None
        a.pair_list = "ETH/USDT,BNB/USDT"
        for pair in ("BTC/USDT", "ETH/USDT", "BNB/USDT"):
            s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=100.0, quantity=1.0, stop_loss_price=50.0, status="open", pair=pair, strategy_id="ema_cross_atr_v1"))
    notifier = FlakyNotifier()
    run_cycle(aid, st, notifier)  # must not raise
    assert sorted(t.status for t in all_trades(aid)) == ["closed", "closed", "closed"]
    assert notifier.attempts >= 3


def test_donchian_and_rsi_exits_survive_failed_messages_too(db, monkeypatch):
    st = make_settings(**dict(KEYS, donchian_enabled=True, rsi_mr_enabled=True, new_strategy_guards_mode="enforce", fee_guard_margin=0.5, max_open_positions=3,
                              max_total_risk_pct=5.0))
    end = int(time.time() * 1000) - 60_000
    pre = base(40)
    rows = [cd(40, 103.0, 103.5, 99.0, 100.5)]  # stops a Donchian long (stop 100) and an RSI long (stop 100) alike
    cs = pre + rows
    data = [[end - (len(cs) - i) * HOUR, c.open, c.high, c.low, c.close, c.volume] for i, c in enumerate(cs)]
    client = spot_client(ohlcv_by_pair={p: data for p in ("BTC/USDT", "ETH/USDT", "BNB/USDT")}, ohlcv=data)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: StrategySignal(SignalType.NONE, "x", 1.0, 1.0, 1.0, 0.01))
    aid = new_account(st)
    opened = (end - HOUR - 5000) / 1000
    from datetime import datetime, timezone
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None
        a.pair_list = "ETH/USDT,BNB/USDT"
        a.extra_strategies = "rsi_mr_v1,donchian_breakout_v1"
        for pair, strat in (("BTC/USDT", "donchian_breakout_v1"), ("ETH/USDT", "rsi_mr_v1"), ("BNB/USDT", "donchian_breakout_v1")):
            s.add(Trade(account_id=aid, side="long", is_paper=True, entry_price=103.0, quantity=1.0, stop_loss_price=100.0, status="open", pair=pair, strategy_id=strat,
                        opened_at=datetime.fromtimestamp(opened, tz=timezone.utc)))
    notifier = FlakyNotifier()
    run_cycle(aid, st, notifier)
    assert sorted(t.status for t in all_trades(aid)) == ["closed", "closed", "closed"]
