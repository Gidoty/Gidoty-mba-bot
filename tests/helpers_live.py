"""Shared harness for the live-trade lifecycle tests (protective stops,
write-ahead entry, reconciliation). Everything exchange-side is the
stateful FakeBinance in tests/fake_binance.py - NOT evidence about real
Binance behaviour, see that module's docstring."""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

from mba_bot import exchange, lifecycle, venue
from mba_bot.config import Settings, load_settings
from mba_bot.crypto_utils import encrypt
from mba_bot.db import session_scope
from mba_bot.indicators import ema
from mba_bot.models import ApiCredential, Customer, Trade, TradingAccount
from mba_bot.notifications import Notifier
from mba_bot.onboarding import start_bot, start_trial
from mba_bot.trader import process_account, reconcile_account

STRATEGY_KWARGS = dict(ema_fast_period=2, ema_slow_period=3, atr_period=2, atr_flat_threshold=0.75, atr_rolling_window=3)


def _crossover_index(closes, *, up: bool):
    fast = ema(closes, STRATEGY_KWARGS["ema_fast_period"])
    slow = ema(closes, STRATEGY_KWARGS["ema_slow_period"])
    for i in range(1, len(closes)):
        if None in (fast[i - 1], slow[i - 1], fast[i], slow[i]):
            continue
        if up and fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            return i
        if not up and fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            return i
    raise AssertionError("no crossover in fixture series")


_UP = [100, 95, 90, 85, 80, 75, 70, 65, 60, 58, 90, 120, 150]
BULLISH_OHLCV = [[i, c, c + 5.0, c - 5.0, c, 1.0] for i, c in enumerate(_UP[: _crossover_index(_UP, up=True) + 1])]
# The bullish series (entry at 90, ATR 23.5, stop 43.0) continued with a
# fall whose fast EMA crosses back under the slow one at close 84 - a
# bearish crossover that is still ABOVE the stop, so it exercises the
# strategy exit, not the stop.
_BEAR_CLOSES = [r[4] for r in BULLISH_OHLCV] + [96, 92, 88, 84]
BEARISH_OHLCV = [[i, c, c + 5.0, c - 5.0, c, 1.0] for i, c in enumerate(_BEAR_CLOSES)]
FLAT_OHLCV = [[i, 100.0, 100.5, 99.5, 100.0, 1.0] for i in range(20)]
ENTRY_PRICE = BULLISH_OHLCV[-1][4]
BEAR_PRICE = BEARISH_OHLCV[-1][4]


class FakeNotifier(Notifier):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, target: str, message: str) -> None:
        self.sent.append((target, message))

    @property
    def texts(self) -> list[str]:
        return [m for _, m in self.sent]


def make_settings(**overrides) -> Settings:
    base = dataclasses.replace(
        load_settings(),
        database_url="sqlite:///:memory:",
        encryption_key=Fernet.generate_key().decode("utf-8"),
        timeframe="1h",
        ema_fast_period=STRATEGY_KWARGS["ema_fast_period"],
        ema_slow_period=STRATEGY_KWARGS["ema_slow_period"],
        atr_period=STRATEGY_KWARGS["atr_period"],
        atr_stop_multiplier=2.0,
        atr_flat_threshold=STRATEGY_KWARGS["atr_flat_threshold"],
        atr_rolling_window=STRATEGY_KWARGS["atr_rolling_window"],
        risk_pct_default=1.0,
        risk_pct_max=2.0,
        max_notional_multiplier=3.0,
        telegram_bot_token="test-bot-token",
        cookie_secure=False,
        protection_max_attempts=3,
        protection_retry_delay_seconds=0.0,
        entry_resolve_attempts=2,
        entry_resolve_delay_seconds=0.0,
        entry_unknown_void_after_seconds=180,
        spot_stop_limit_buffer_pct=0.5,
        protection_software_grace_pct=0.5,
        # The protection guards run ENFORCED in every live-path test, so a regression that wrongly blocks (or fails
        # to block) a normal entry shows up everywhere. The synthetic candles here put the stop ~52% from entry, which
        # is safe only at 1x leverage: that is the fixtures' leverage (futures_client below).
        protection_precheck_mode="enforce",
        protection_fail_limit_mode="enforce",
    )
    return dataclasses.replace(base, **overrides)


def make_account(
    session,
    *,
    encryption_key: str,
    email: str = "live@example.com",
    market_type: str = "spot",
    pair: str = "BTC/USDT",
    allocated_capital: float | None = 1000.0,
    notification_target: str | None = "555000111",
) -> str:
    customer = Customer(email=email, password_hash="x", has_paid=True)
    session.add(customer)
    session.flush()
    now = datetime.now(timezone.utc)
    account = TradingAccount(
        customer_id=customer.id,
        bot_code=f"MBA-{abs(hash(email)) % 999999:06d}",
        pair=pair,
        market_type=market_type,
        notification_channel="telegram",
        notification_target=notification_target,
        trial_ends_at=start_trial(now=now - timedelta(days=30), trial_days=7),
        live_trading_enabled_at=now,
        bot_started_at=start_bot(now=now),
        allocated_capital=allocated_capital,
    )
    session.add(account)
    session.flush()
    session.add(
        ApiCredential(
            account_id=account.id,
            exchange="binance",
            environment="mainnet",
            encrypted_api_key=encrypt(f"synthetic-{email}-key", key=encryption_key),
            encrypted_api_secret=encrypt(f"synthetic-{email}-secret", key=encryption_key),
        )
    )
    return account.id


def install(monkeypatch, client) -> None:
    """Route the bot to `client`, and make every retry delay instant."""
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(venue, "_sleep", _no_sleep)
    monkeypatch.setattr(lifecycle, "_sleep", _no_sleep)


def run_cycle(
    account_id: str, settings: Settings, notifier: FakeNotifier | None = None, *, swallow: bool = False
) -> FakeNotifier:
    """One scheduler cycle. `swallow=True` mimics scheduler._run_one, which
    catches the cycle's exception INSIDE the session scope (so what
    process_account recorded - e.g. last_error - is still committed)."""
    notifier = notifier or FakeNotifier()

    async def go():
        # The scheduler's own session mode: no implicit flushes (see db.session_scope), so every
        # lifecycle/trader test also proves the engine does not depend on autoflush.
        with session_scope(autoflush=False) as session:
            account = session.get(TradingAccount, account_id)
            if swallow:
                try:
                    await process_account(session, account, settings, notifier)
                except Exception:
                    pass
            else:
                await process_account(session, account, settings, notifier)

    asyncio.run(go())
    return notifier


def run_reconcile(account_id: str, settings: Settings, notifier: FakeNotifier | None = None) -> FakeNotifier:
    notifier = notifier or FakeNotifier()

    async def go():
        with session_scope(autoflush=False) as session:
            account = session.get(TradingAccount, account_id)
            await reconcile_account(session, account, settings, notifier)

    asyncio.run(go())
    return notifier


def the_trade(account_id: str) -> Trade:
    with session_scope() as session:
        return session.query(Trade).filter(Trade.account_id == account_id).order_by(Trade.opened_at.desc()).first()


def all_trades(account_id: str) -> list[Trade]:
    with session_scope() as session:
        return session.query(Trade).filter(Trade.account_id == account_id).all()


def spot_client(**kw) -> "FakeBinance":
    from fake_binance import FakeBinance

    kw.setdefault("ohlcv", BULLISH_OHLCV)
    kw.setdefault("min_notional", 5.0)
    return FakeBinance(market_type="spot", **kw)


def futures_client(**kw) -> "FakeBinance":
    from fake_binance import FakeBinance

    kw.setdefault("ohlcv", BULLISH_OHLCV)
    kw.setdefault("min_notional", 5.0)
    kw.setdefault("leverage_by_symbol", {"BTC/USDT": 1.0})
    return FakeBinance(market_type="futures", **kw)


def new_account(settings, **kw) -> str:
    with session_scope() as session:
        return make_account(session, encryption_key=settings.encryption_key, **kw)


def open_protected(monkeypatch, *, market="spot", settings=None, **client_kw):
    """Drive one real cycle to a verified, protected, open live trade."""
    settings = settings or make_settings(max_notional_multiplier=100.0)
    client = (spot_client if market == "spot" else futures_client)(**client_kw)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type=market)
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected", trade.status
    return client, settings, account_id


def live_context(session, account_id, client, settings, notifier=None, *, entries_allowed=False):
    """A LiveContext + prepared venue over `client`, for driving lifecycle functions directly."""
    from datetime import datetime, timezone

    from mba_bot import lifecycle
    from mba_bot.venue import build_venue

    account = session.get(TradingAccount, account_id)
    v = build_venue(client, market_type=account.market_type, pair=account.pair, entries_allowed=entries_allowed)
    return lifecycle.LiveContext(
        session=session, account=account, venue=v, settings=settings, notifier=notifier or FakeNotifier(),
        now=datetime.now(timezone.utc),
    )
