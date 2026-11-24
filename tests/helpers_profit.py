"""Shared fixtures for the profit-protection tests (Parts A to J): a protected live Futures trade (entry 90, initial stop 43, R = 47) with fixed hourly candles, and helpers to set the market. Not a test file."""
from __future__ import annotations

from datetime import datetime, timezone

from helpers_live import BULLISH_OHLCV, make_settings, open_protected, the_trade
from test_donchian_integration import NO_EMA

from mba_bot import trader
from mba_bot.db import session_scope
from mba_bot.models import Trade

HOUR = 3_600_000
U = timezone.utc


def live(monkeypatch, **over):
    """A protected live Futures trade (entry 90, initial stop 43, R = 47), backdated so the candles built next come AFTER the entry and are closed. The EMA signal is silenced afterwards."""
    settings = make_settings(max_notional_multiplier=100.0, **over)
    client, settings, aid = open_protected(monkeypatch, market="futures", settings=settings)
    monkeypatch.setattr(trader, "generate_signal", lambda *a, **k: NO_EMA)
    now_ms = int(datetime.now(U).timestamp() * 1000)
    base = (now_ms // HOUR) * HOUR - 8 * HOUR
    with session_scope() as s:
        t = s.get(Trade, the_trade(aid).id)
        t.opened_at = datetime.fromtimestamp((base - 1_800_000) / 1000, tz=U)
    t = the_trade(aid)
    assert (t.entry_price, t.initial_stop_price, t.stop_loss_price) == (90.0, 43.0, 43.0)
    return client, settings, aid, base


def candles(client, base, rows):
    client._ohlcv = BULLISH_OHLCV + [[base + i * HOUR, o, h, lo, cl, 1.0] for i, (o, h, lo, cl) in enumerate(rows)]
    client.price = rows[-1][3]


def resting_stops(client):
    return [o for o in client.orders_by_role("stop") if o["status"] == "open"]
