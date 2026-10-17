"""Unit tests for the persisted runtime-status fields (last_scanned_at,
last_signal, last_error, ...) that trader.py now writes every cycle, plus
the one-time Telegram notifications. No real network/exchange calls - the
ccxt client is replaced with a small fake.

Test bodies are async (process_account is a coroutine) but the test
functions themselves are plain sync + asyncio.run(), rather than adding
pytest-asyncio as a new dependency for one test module.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from mba_bot import exchange
from mba_bot.config import Settings
from mba_bot.crypto_utils import encrypt
from mba_bot.db import init_db, session_scope
from mba_bot.models import ApiCredential, Customer, TradingAccount
from mba_bot.notifications import Notifier
from mba_bot.trader import process_account

FLAT_OHLCV = [[i, 100.0, 101.0, 99.0, 100.0, 1.0] for i in range(10)]
# ema_fast_period=2/ema_slow_period=3 matches test_strategy.py's STRATEGY_KWARGS -
# enough history for a real (non-None) evaluation; flat prices never cross,
# so this always resolves to SignalType.NONE, "no crossover".


class FakeNotifier(Notifier):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, target: str, message: str) -> None:
        self.sent.append((target, message))


class FakeExchangeClient:
    def __init__(self, *, ohlcv=None, raise_on_fetch: Exception | None = None):
        self._ohlcv = FLAT_OHLCV if ohlcv is None else ohlcv
        self._raise_on_fetch = raise_on_fetch
        self.closed = False

    async def fetch_ohlcv(self, pair, timeframe, limit):
        if self._raise_on_fetch is not None:
            raise self._raise_on_fetch
        return self._ohlcv

    async def fetch_balance(self):
        return {"free": {"USDT": 10_000.0}, "info": {}}

    async def close(self):
        self.closed = True


def _settings(**overrides) -> Settings:
    defaults = dict(
        database_url="sqlite:///:memory:",
        encryption_key=Fernet.generate_key().decode("utf-8"),
        poll_interval_seconds=300,
        timeframe="1h",
        ohlcv_limit=200,
        trial_days=7,
        ema_fast_period=2,
        ema_slow_period=3,
        atr_period=2,
        atr_stop_multiplier=2.0,
        atr_flat_threshold=0.75,
        atr_rolling_window=3,
        risk_pct_default=1.0,
        risk_pct_max=2.0,
        testnet_api_key="testnet-key",
        testnet_api_secret="testnet-secret",
        paper_starting_balance=10_000.0,
        telegram_bot_token="test-bot-token",
        telegram_bot_username="mba_test_bot",
        whatsapp_provider_token=None,
        jwt_secret="test-jwt-secret",
        jwt_algorithm="HS256",
        jwt_expires_minutes=10080,
        frontend_origin="http://localhost:5173",
        cookie_secure=False,
        paystack_secret_key=None,
        paystack_amount=0,
        paystack_currency="NGN",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture()
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _make_account(session, *, email, has_paid, is_paper, encryption_key: str) -> str:
    customer = Customer(email=email, password_hash="x", has_paid=has_paid)
    session.add(customer)
    session.flush()

    now = datetime.now(timezone.utc)
    account = TradingAccount(
        customer_id=customer.id,
        bot_code=f"MBA-{email[:6].upper()}",
        pair="BTC/USDT",
        market_type="spot",
        notification_channel="telegram",
        notification_target="555000111",
        trial_ends_at=(now + timedelta(days=7)) if is_paper else (now - timedelta(days=1)),
    )
    session.add(account)
    session.flush()

    session.add(
        ApiCredential(
            account_id=account.id,
            exchange="binance",
            encrypted_api_key=encrypt("mainnet-key", key=encryption_key),
            encrypted_api_secret=encrypt("mainnet-secret", key=encryption_key),
        )
    )
    return account.id


def test_successful_cycle_records_scan_signal_and_clears_error(db, monkeypatch):
    settings = _settings()
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: FakeExchangeClient())

    with session_scope() as session:
        account_id = _make_account(
            session, email="paper1@example.com", has_paid=False, is_paper=True, encryption_key=settings.encryption_key
        )

    async def run():
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            account.last_error = "a stale error from a previous cycle"
            notifier = FakeNotifier()
            await process_account(session, account, settings, notifier)

            assert account.last_scanned_at is not None
            assert account.last_signal == "none"
            assert account.last_signal_at is not None
            assert account.last_error is None
            assert account.last_error_at is None

    asyncio.run(run())


def test_empty_market_data_records_a_safe_error_and_notifies_once(db, monkeypatch):
    settings = _settings()
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: FakeExchangeClient(ohlcv=[]))

    with session_scope() as session:
        account_id = _make_account(
            session, email="paper2@example.com", has_paid=False, is_paper=True, encryption_key=settings.encryption_key
        )

    notifier = FakeNotifier()

    async def run():
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            await process_account(session, account, settings, notifier)
            assert account.last_error is not None
            assert account.last_scanned_at is not None

        # A second cycle with the same ongoing problem must not notify again.
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            await process_account(session, account, settings, notifier)

    asyncio.run(run())

    error_notifications = [m for _, m in notifier.sent if "hit a problem" in m]
    assert len(error_notifications) == 1


def test_recovering_from_an_error_clears_it(db, monkeypatch):
    settings = _settings()

    with session_scope() as session:
        account_id = _make_account(
            session, email="paper3@example.com", has_paid=False, is_paper=True, encryption_key=settings.encryption_key
        )

    notifier = FakeNotifier()

    async def run():
        monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: FakeExchangeClient(ohlcv=[]))
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            await process_account(session, account, settings, notifier)
            assert account.last_error is not None

        monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: FakeExchangeClient())
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            await process_account(session, account, settings, notifier)
            assert account.last_error is None

    asyncio.run(run())


def test_missing_credentials_records_error_without_raising(db, monkeypatch):
    settings = _settings(testnet_api_key=None, testnet_api_secret=None)
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: FakeExchangeClient())

    with session_scope() as session:
        account_id = _make_account(
            session, email="paper4@example.com", has_paid=False, is_paper=True, encryption_key=settings.encryption_key
        )

    notifier = FakeNotifier()

    async def run():
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            await process_account(session, account, settings, notifier)
            assert account.last_error is not None
            assert "credential" in account.last_error.lower()

    asyncio.run(run())
    assert len(notifier.sent) == 1


def test_unexpected_exception_is_recorded_safely_and_still_reraised(db, monkeypatch):
    settings = _settings()
    boom = RuntimeError("leaking internal detail: db password hunter2")
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: FakeExchangeClient(raise_on_fetch=boom))

    with session_scope() as session:
        account_id = _make_account(
            session, email="paper5@example.com", has_paid=False, is_paper=True, encryption_key=settings.encryption_key
        )

    notifier = FakeNotifier()

    async def run():
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            with pytest.raises(RuntimeError):
                await process_account(session, account, settings, notifier)
            assert account.last_error is not None
            assert "hunter2" not in account.last_error

    asyncio.run(run())


def test_going_live_sends_a_one_time_activation_message(db, monkeypatch):
    settings = _settings()
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: FakeExchangeClient())

    with session_scope() as session:
        account_id = _make_account(
            session, email="live1@example.com", has_paid=True, is_paper=False, encryption_key=settings.encryption_key
        )

    notifier = FakeNotifier()

    async def run():
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            await process_account(session, account, settings, notifier)
            assert account.live_notified_at is not None

        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            await process_account(session, account, settings, notifier)

    asyncio.run(run())

    activation_messages = [m for _, m in notifier.sent if "now live" in m]
    assert len(activation_messages) == 1


def test_unlinked_account_is_skipped_and_untouched(db):
    settings = _settings()
    with session_scope() as session:
        customer = Customer(email="unlinked@example.com", password_hash="x", has_paid=False)
        session.add(customer)
        session.flush()
        account = TradingAccount(
            customer_id=customer.id,
            pair="BTC/USDT",
            market_type="spot",
            notification_channel="telegram",
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        session.add(account)
        session.flush()
        account_id = account.id

    notifier = FakeNotifier()

    async def run():
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            await process_account(session, account, settings, notifier)
            assert account.last_scanned_at is None
            assert notifier.sent == []

    asyncio.run(run())
