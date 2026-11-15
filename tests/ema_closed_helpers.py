"""A fixed replay of hourly candles through the real single-trade cycle with a frozen clock, for the EMA closed-candle tests.

Step k serves candles[0..k] where candle k is still FORMING (the clock is `offsets` minutes into it, 30 by default), exactly what Binance returns."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from helpers_live import FakeNotifier, install, make_settings, new_account, spot_client

from mba_bot import trader
from mba_bot.db import init_db, session_scope
from mba_bot.models import DecisionLog, Trade, TradingAccount

HOUR = 3_600_000
T0 = int(datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
CLOSES = [100, 99, 98, 97, 96, 95, 94, 93, 95, 98, 102, 105, 104, 100, 96, 92, 93, 97, 101, 105, 108, 106, 101, 96, 90, 91, 95, 100, 106, 110]
KEYS = dict(testnet_spot_api_key="paper-key-1234567890", testnet_spot_api_secret="paper-secret-1234567890")


def candles(closes=CLOSES, t0=T0):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append([t0 + i * HOUR, float(o), float(max(o, c) + 2), float(min(o, c) - 2), float(c), 10.0])
    return out


class Clock:
    """Replaces trader.datetime so that datetime.now(tz) is a controllable instant."""

    def __init__(self):
        self.instant = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def install(self, monkeypatch):
        clock = self

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.instant if tz is not None else clock.instant.replace(tzinfo=None)

        monkeypatch.setattr(trader, "datetime", Frozen)


def new_paper_account(settings):
    aid = new_account(settings)
    with session_scope() as s:
        s.get(TradingAccount, aid).bot_started_at = None
    return aid


def replay(tmp_path, monkeypatch, overrides=None, closes=CLOSES, first_step=8, pre_hook=None, offsets=(30,)):
    """Run the cycle once per step. Returns a list of per-step snapshots and the decision-log rows."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    init_db(f"sqlite:///{tmp_path}/replay.db")
    st = make_settings(**dict(KEYS, trade_telemetry=True, max_notional_multiplier=100.0, **(overrides or {})))
    series = candles(closes)
    client = spot_client(ohlcv=series[:1])
    install(monkeypatch, client)
    clock = Clock()
    clock.install(monkeypatch)
    aid = new_paper_account(st)
    if pre_hook:
        pre_hook(aid)
    notifier = FakeNotifier()
    steps = []
    for k in range(first_step, len(series)):
        client._ohlcv = series[: k + 1]
        for minutes in offsets:  # one poll per offset (minutes into candle k), as the 5-minute scheduler would
            clock.instant = datetime.fromtimestamp((series[k][0] + minutes * 60_000) / 1000, tz=timezone.utc)

            async def go():
                with session_scope(autoflush=False) as s:
                    await trader.process_account(s, s.get(TradingAccount, aid), st, notifier)

            asyncio.run(go())
        with session_scope() as s:
            a = s.get(TradingAccount, aid)
            trades = s.query(Trade).filter(Trade.account_id == aid).order_by(Trade.opened_at, Trade.id).all()
            steps.append((k, a.last_signal, [(t.side, t.status, t.close_reason, round(t.entry_price, 4), None if t.exit_price is None else round(t.exit_price, 4)) for t in trades]))
    with session_scope() as s:
        decisions = sorted((d.stage, d.outcome, d.reason_code, d.pair, d.candle_ts, d.signal) for d in s.query(DecisionLog).filter(DecisionLog.account_id == aid).all())
    return steps, decisions, notifier.texts
