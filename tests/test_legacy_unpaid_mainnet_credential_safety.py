"""Proves, by actually running the scheduler's real code path (not just
reading it), that an UNPAID account can never place a real Binance order -
including the specific legacy shape under audit: an unpaid account that
already has a valid Mainnet-tagged credential stored (predating the
payment-gating checks added to the credential-write endpoints). This
file never reads, decrypts, prints, or logs any real stored credential -
every credential here is a synthetic value created and encrypted by the
test itself with a fresh, test-local encryption key.

Every test forces a REAL bullish crossover signal (not a flat "no
signal" series) via the same closes/EMA-period combination
test_strategy.py already proves produces SignalType.BULLISH_CROSSOVER,
so a trade entry is actually attempted on every run - proving the
no-live-order guarantee holds under the one condition where it would
otherwise matter, not merely when there's nothing to trade.
"""
from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

from mba_bot import exchange, scheduler
from mba_bot.config import Settings
from mba_bot.crypto_utils import encrypt
from mba_bot.db import init_db, list_active_accounts, session_scope
from mba_bot.indicators import ema
from mba_bot.models import ApiCredential, Customer, TradingAccount
from mba_bot.notifications import Notifier
from mba_bot.onboarding import TradingMode, enable_live_trading, resolve_trading_mode, start_bot, start_trial
from mba_bot.trader import process_account

from fake_binance import FakeBinance

STRATEGY_KWARGS = dict(ema_fast_period=2, ema_slow_period=3, atr_period=2, atr_flat_threshold=0.75, atr_rolling_window=3)


def _find_crossover_index(closes, *, fast_period, slow_period):
    """Same approach as test_strategy.py::find_crossover_index - a
    crossover is a transition detected at a specific bar, so the candle
    window must END exactly there, not merely contain it somewhere
    earlier in the history."""
    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    for i in range(1, len(closes)):
        if None in (fast[i - 1], slow[i - 1], fast[i], slow[i]):
            continue
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            return i
    raise AssertionError("no bullish crossover found in the fixture closes series")


# Same closes series test_strategy.py::test_bullish_crossover_detected uses,
# trimmed to end exactly on the crossover bar - a real, actionable buy
# signal, not a no-op "none" evaluation.
_ALL_CLOSES = [100, 95, 90, 85, 80, 75, 70, 65, 60, 58, 90, 120, 150]
_CROSSOVER_IDX = _find_crossover_index(
    _ALL_CLOSES, fast_period=STRATEGY_KWARGS["ema_fast_period"], slow_period=STRATEGY_KWARGS["ema_slow_period"]
)
_BULLISH_CLOSES = _ALL_CLOSES[: _CROSSOVER_IDX + 1]
BULLISH_OHLCV = [[i, c, c + 5.0, c - 5.0, c, 1.0] for i, c in enumerate(_BULLISH_CLOSES)]


class FakeNotifier(Notifier):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, target: str, message: str) -> None:
        self.sent.append((target, message))


class OrderTrackingExchangeClient(FakeBinance):
    """A stateful FakeBinance (tests/fake_binance.py) that also records
    the api_key it was constructed with (a synthetic test value, never a
    real credential) and whether sandbox mode was requested, so a test can
    assert the platform testnet credential was used instead of the
    account's own stored one. `market_orders` lists every non-conditional
    order placed - the thing that means a real order was submitted."""

    def __init__(self, *, api_key, sandbox, ohlcv=None):
        super().__init__(
            api_key=api_key,
            sandbox=sandbox,
            ohlcv=BULLISH_OHLCV if ohlcv is None else ohlcv,
            free_balance={"USDT": 50_000.0},
        )


def _settings(**overrides) -> Settings:
    defaults = dict(
        database_url="sqlite:///:memory:",
        encryption_key=Fernet.generate_key().decode("utf-8"),
        poll_interval_seconds=300,
        timeframe="1h",
        ohlcv_limit=200,
        trial_days=7,
        ema_fast_period=STRATEGY_KWARGS["ema_fast_period"],
        ema_slow_period=STRATEGY_KWARGS["ema_slow_period"],
        atr_period=STRATEGY_KWARGS["atr_period"],
        atr_stop_multiplier=2.0,
        atr_flat_threshold=STRATEGY_KWARGS["atr_flat_threshold"],
        atr_rolling_window=STRATEGY_KWARGS["atr_rolling_window"],
        risk_pct_default=1.0,
        risk_pct_max=2.0,
        max_notional_multiplier=3.0,
        testnet_spot_api_key="PLATFORM-TESTNET-SPOT-KEY",
        testnet_spot_api_secret="PLATFORM-TESTNET-SPOT-SECRET",
        demo_futures_api_key="PLATFORM-DEMO-FUTURES-KEY",
        demo_futures_api_secret="PLATFORM-DEMO-FUTURES-SECRET",
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
    # Guards run enforced here too (see helpers_live.make_settings); the synthetic stop (~52%) needs 1x leverage.
    defaults.setdefault("protection_precheck_mode", "enforce")
    defaults.setdefault("protection_fail_limit_mode", "enforce")
    defaults.update(overrides)
    return Settings(**defaults)


def _make_account(
    session,
    *,
    email: str,
    has_paid: bool,
    encryption_key: str,
    market_type: str = "spot",
    trial_expired: bool = False,
    live_bypass_accepted: bool = False,
    live_trading_enabled: bool = False,
    credential_environment: str | None = "mainnet",
    is_active: bool = True,
    bot_started: bool = False,
    allocated_capital: float | None = None,
) -> str:
    """credential_environment=None means no stored credential at all;
    any other value stores one with a SYNTHETIC key/secret the test
    itself generates - never a real credential."""
    customer = Customer(email=email, password_hash="x", has_paid=has_paid)
    session.add(customer)
    session.flush()

    now = datetime.now(timezone.utc)
    account = TradingAccount(
        customer_id=customer.id,
        bot_code=f"MBA-{abs(hash(email)) % 999999:06d}",
        pair="BTC/USDT",
        market_type=market_type,
        notification_channel="telegram",
        notification_target="555000111",
        trial_ends_at=start_trial(now=now - timedelta(days=30), trial_days=7) if trial_expired else start_trial(now=now, trial_days=7),
        live_bypass_accepted_at=now if live_bypass_accepted else None,
        live_trading_enabled_at=enable_live_trading(now=now) if live_trading_enabled else None,
        is_active=is_active,
        bot_started_at=start_bot(now=now) if bot_started else None,
        allocated_capital=allocated_capital,
    )
    session.add(account)
    session.flush()

    if credential_environment is not None:
        session.add(
            ApiCredential(
                account_id=account.id,
                exchange="binance",
                environment=credential_environment,
                encrypted_api_key=encrypt("synthetic-test-mainnet-key", key=encryption_key),
                encrypted_api_secret=encrypt("synthetic-test-mainnet-secret", key=encryption_key),
            )
        )
    return account.id


def _run_cycle(account_id: str, settings: Settings) -> None:
    async def go():
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            notifier = FakeNotifier()
            await process_account(session, account, settings, notifier)

    asyncio.run(go())


# --- Q1/Q2/Q8: the exact legacy scenario under audit ---


def test_legacy_unpaid_account_with_stored_mainnet_credential_places_no_order_on_a_real_buy_signal(tmp_path, monkeypatch):
    """Unpaid, trial long expired (eligible by time), bypass never
    accepted, live trading never explicitly enabled, but a valid
    Mainnet-tagged credential already on file - exactly the legacy
    shape under audit. A real bullish crossover signal fires. No order
    may be placed, and the exchange client that IS used must be the
    platform testnet one, never the stored credential."""
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    captured = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        client = OrderTrackingExchangeClient(api_key=kwargs["api_key"], sandbox=kwargs["sandbox"])
        captured["client"] = client
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", fake_builder)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="legacy-unpaid-mainnet@example.com",
            has_paid=False,
            encryption_key=settings.encryption_key,
            trial_expired=True,
            credential_environment="mainnet",
        )

    _run_cycle(account_id, settings)

    client = captured["client"]
    assert client.market_orders == [], "a real order was submitted for an unpaid account"
    assert captured["sandbox"] is True, "exchange client was not built in sandbox/testnet mode"
    assert captured["api_key"] == settings.testnet_spot_api_key, "the platform testnet key was not used"
    assert captured["api_key"] != "synthetic-test-mainnet-key", "the account's own stored credential leaked into the live client"

    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        trades = list(account.trades)
        assert len(trades) == 1
        assert trades[0].is_paper is True, "a live (non-paper) trade row was created for an unpaid account"


# --- Q3: resolve_trading_mode structurally can't reach LIVE with has_paid=False ---


def test_resolve_trading_mode_never_resolves_live_when_unpaid_under_any_other_combination():
    now = datetime.now(timezone.utc)
    trial_ends_at = now - timedelta(days=1)  # expired, i.e. eligible by time
    for bypass_accepted, live_enabled, mainnet_cred, bot_started in itertools.product([False, True], repeat=4):
        state = resolve_trading_mode(
            trial_ends_at=trial_ends_at,
            live_bypass_accepted_at=now if bypass_accepted else None,
            has_paid=False,
            live_trading_enabled_at=now if live_enabled else None,
            has_mainnet_credential=mainnet_cred,
            bot_started_at=now if bot_started else None,
            now=now,
        )
        assert state.mode == TradingMode.PAPER, (
            f"unpaid account resolved to {state.mode} with bypass_accepted={bypass_accepted}, "
            f"live_enabled={live_enabled}, mainnet_cred={mainnet_cred}, bot_started={bot_started}"
        )

    # Control: the same eligibility/enabled/credential/started
    # combination, paid, genuinely resolves LIVE - proving the test
    # above isn't vacuous (resolve_trading_mode isn't just
    # broken/always-paper).
    live_state = resolve_trading_mode(
        trial_ends_at=trial_ends_at,
        live_bypass_accepted_at=now,
        has_paid=True,
        live_trading_enabled_at=now,
        has_mainnet_credential=True,
        bot_started_at=now,
        now=now,
    )
    assert live_state.mode == TradingMode.LIVE


# --- Q9: payment alone cannot place an order ---


def test_payment_alone_places_no_order(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    captured = {}

    def fake_builder(**kwargs):
        client = OrderTrackingExchangeClient(api_key=kwargs["api_key"], sandbox=kwargs["sandbox"])
        captured["client"] = client
        captured["sandbox"] = kwargs["sandbox"]
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", fake_builder)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="paid-only@example.com",
            has_paid=True,
            encryption_key=settings.encryption_key,
            credential_environment="testnet",  # no mainnet credential connected yet
            trial_expired=False,  # not even eligible yet
            live_trading_enabled=False,
        )

    _run_cycle(account_id, settings)

    assert captured["client"].market_orders == []
    assert captured["sandbox"] is True


# --- Q10: a Mainnet credential alone (nothing else) cannot place an order ---


def test_mainnet_credential_alone_places_no_order(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    captured = {}

    def fake_builder(**kwargs):
        client = OrderTrackingExchangeClient(api_key=kwargs["api_key"], sandbox=kwargs["sandbox"])
        captured["client"] = client
        captured["sandbox"] = kwargs["sandbox"]
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", fake_builder)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="mainnet-only@example.com",
            has_paid=False,
            encryption_key=settings.encryption_key,
            credential_environment="mainnet",
            trial_expired=False,  # trial still running - not eligible, no bypass either
            live_trading_enabled=False,
        )

    _run_cycle(account_id, settings)

    assert captured["client"].market_orders == []
    assert captured["sandbox"] is True


# --- Q11: live_trading_enabled_at alone (without payment) cannot place an order ---
# This can never happen through the real API (enable-live-trading 409s
# without has_paid first - see test_api_live_trading_safety.py), so this
# test constructs the field directly, simulating stale/corrupted data or
# a bypassed API layer, to prove resolve_trading_mode's own has_paid
# check is what actually stops it - not merely that the API happens to
# order its checks correctly.


def test_live_authorization_alone_without_payment_places_no_order(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    captured = {}

    def fake_builder(**kwargs):
        client = OrderTrackingExchangeClient(api_key=kwargs["api_key"], sandbox=kwargs["sandbox"])
        captured["client"] = client
        captured["sandbox"] = kwargs["sandbox"]
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", fake_builder)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="live-flag-only@example.com",
            has_paid=False,
            encryption_key=settings.encryption_key,
            credential_environment="mainnet",
            trial_expired=True,
            live_trading_enabled=True,  # forced directly - never reachable via the real API while unpaid
        )

    _run_cycle(account_id, settings)

    assert captured["client"].market_orders == []
    assert captured["sandbox"] is True


# --- Q4/Q12: the scheduler's own "is this account even running" gate ---


def test_paused_account_is_excluded_from_the_scheduler_entirely(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()

    with session_scope() as session:
        paused_id = _make_account(
            session,
            email="paused-fully-authorized@example.com",
            has_paid=True,
            encryption_key=settings.encryption_key,
            credential_environment="mainnet",
            trial_expired=True,
            live_trading_enabled=True,
            is_active=False,  # the "Start Bot" equivalent today - explicitly turned off
        )

    with session_scope() as session:
        selected_ids = [a.id for a in list_active_accounts(session)]
    assert paused_id not in selected_ids, "a paused account was selected for a scheduler cycle"

    # Defense in depth: even if _run_one were ever called directly for a
    # paused account id (e.g. a stale id from a prior listing), it must
    # still refuse to process it.
    asyncio.run(scheduler._run_one(paused_id, settings))
    with session_scope() as session:
        account = session.get(TradingAccount, paused_id)
        assert account.last_scanned_at is None, "a paused account was processed despite is_active=False"


def test_fully_authorized_and_active_account_can_reach_live_order_placement(tmp_path, monkeypatch):
    """The positive control for the whole file: proves the gates above
    aren't accidentally blocking legitimate live trading too. Every
    condition satisfied (paid, eligible, mainnet credential, live
    trading enabled, bot explicitly started, trading capital
    configured) AND the account is active - only then does a real buy
    signal result in create_market_order actually being called, using
    the account's own credential path (sandbox=False), not the
    platform testnet one."""
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    captured = {}

    def fake_builder(**kwargs):
        client = OrderTrackingExchangeClient(api_key=kwargs["api_key"], sandbox=kwargs["sandbox"])
        captured["client"] = client
        captured["sandbox"] = kwargs["sandbox"]
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", fake_builder)
    monkeypatch.setattr(scheduler, "build_notifier", lambda *args, **kwargs: FakeNotifier())

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="fully-authorized-active@example.com",
            has_paid=True,
            encryption_key=settings.encryption_key,
            credential_environment="mainnet",
            trial_expired=True,
            live_trading_enabled=True,
            is_active=True,
            bot_started=True,
            allocated_capital=10_000.0,
        )

    asyncio.run(scheduler._run_one(account_id, settings))

    assert captured["sandbox"] is False
    assert len(captured["client"].market_orders) == 1, "a fully-authorized, active account failed to place its real order"
