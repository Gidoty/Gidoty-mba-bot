"""Audit L4: RSI_MR_TIMEFRAME and DONCHIAN_TIMEFRAME are validated. Anything the bot cannot size a candle for is rejected: it falls back to 1h and is logged. Before, '1w'
fell back to one hour in the closed-candle test while the exchange was asked for weekly candles, so a forming candle was treated as closed."""
from __future__ import annotations

import logging
import time

import pytest
from helpers_live import all_trades, install, make_settings, new_account, run_cycle, spot_client
from test_donchian import with_last

from mba_bot import rsi_mr, trader
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.models import TradingAccount
from mba_bot.strategy import SignalType, StrategySignal

HOUR = 3_600_000


@pytest.mark.parametrize("raw,want", [("1h", "1h"), (" 4H ", "4h"), ("15m", "15m"), ("1d", "1d"), ("1w", "1h"), ("1M", "1h"), ("90m", "1h"), ("abc", "1h"), ("", "1h"), ("0h", "1h"), ("-1h", "1h"),
                                      ("1h30m", "1h"), ("100h", "1h")])
@pytest.mark.parametrize("name", ["RSI_MR_TIMEFRAME", "DONCHIAN_TIMEFRAME"])
def test_the_environment_value_is_validated(monkeypatch, caplog, name, raw, want):
    monkeypatch.setenv(name, raw)
    with caplog.at_level(logging.WARNING):
        s = load_settings()
    got = s.rsi_mr_timeframe if name == "RSI_MR_TIMEFRAME" else s.donchian_timeframe
    assert got == want
    if raw.strip() and want == "1h" and raw.strip().lower() != "1h":
        assert any(name in r.getMessage() for r in caplog.records), "an unsupported value must be logged"


def test_the_supported_set_and_the_helper():
    assert rsi_mr.SUPPORTED_TIMEFRAMES == ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d")
    assert rsi_mr.normalize_timeframe("4H") == ("4h", True) and rsi_mr.normalize_timeframe("1w") == ("1h", False) and rsi_mr.normalize_timeframe(None) == ("1h", False)


def test_a_settings_object_with_an_unsupported_timeframe_still_asks_the_exchange_for_1h(tmp_path, monkeypatch):
    """Defence in depth: even if a Settings object is built by hand with '1w', the candles asked for and the closed-candle test use the same 1h."""
    init_db(f"sqlite:///{tmp_path}/t.db")
    st = make_settings(testnet_spot_api_key="k" * 12, testnet_spot_api_secret="s" * 12, rsi_mr_enabled=True, rsi_mr_timeframe="1w", new_strategy_guards_mode="enforce",
                       fee_guard_margin=0.5, max_notional_multiplier=100.0)
    client = spot_client()
    asked = []
    real = client.fetch_ohlcv

    async def spy(symbol, timeframe=None, **kw):
        asked.append(timeframe)
        return await real(symbol, timeframe=timeframe, **kw)

    monkeypatch.setattr(client, "fetch_ohlcv", spy)
    install(monkeypatch, client)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: StrategySignal(SignalType.NONE, "x", 1.0, 1.0, 1.0, 0.01))
    aid = new_account(st)
    with session_scope() as s:
        a = s.get(TradingAccount, aid)
        a.bot_started_at = None
        a.extra_strategies = "rsi_mr_v1"
    run_cycle(aid, st)
    assert "1w" not in asked and "1h" in asked
