"""Backend tests for the capital-allocation, Start Bot lifecycle, and
Auto Select pair-selection work. Each test runs the real scheduler code
path (`trader.process_account`), not just unit-level helpers, mirroring
test_legacy_unpaid_mainnet_credential_safety.py's approach: a fake ccxt
client stands in for the network, and every crossover signal is a real
one forced via the same `_find_crossover_index` trimming technique, so
an entry is actually attempted wherever the test needs one.

The most important test here is
`test_open_live_trade_keeps_being_managed_with_real_credentials_even_if_the_account_is_newly_forced_to_paper`:
it proves the fix for the exact migration-triggered danger the capital-
control review surfaced - adding the `bot_started_at` gate makes every
pre-existing live account's *new-entry* mode flip to PAPER, and without
this fix an already-open real position would have silently been
"managed" against testnet credentials/market data and never received
its real closing order.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from mba_bot import exchange
from mba_bot.config import SUPPORTED_PAIRS, Settings
from mba_bot.crypto_utils import encrypt
from mba_bot.db import init_db, list_active_accounts, session_scope
from mba_bot.indicators import ema
from mba_bot.models import ApiCredential, Customer, Trade, TradingAccount
from mba_bot.notifications import Notifier
from mba_bot.onboarding import start_bot, start_trial
from mba_bot.trader import process_account

from fake_binance import FLAT_OHLCV as _FAKE_FLAT, FakeBinance

STRATEGY_KWARGS = dict(ema_fast_period=2, ema_slow_period=3, atr_period=2, atr_flat_threshold=0.75, atr_rolling_window=3)


def _find_crossover_index(closes, *, fast_period, slow_period):
    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    for i in range(1, len(closes)):
        if None in (fast[i - 1], slow[i - 1], fast[i], slow[i]):
            continue
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            return i
    raise AssertionError("no bullish crossover found in the fixture closes series")


_ALL_CLOSES = [100, 95, 90, 85, 80, 75, 70, 65, 60, 58, 90, 120, 150]
_CROSSOVER_IDX = _find_crossover_index(
    _ALL_CLOSES, fast_period=STRATEGY_KWARGS["ema_fast_period"], slow_period=STRATEGY_KWARGS["ema_slow_period"]
)
_BULLISH_CLOSES = _ALL_CLOSES[: _CROSSOVER_IDX + 1]
BULLISH_OHLCV = [[i, c, c + 5.0, c - 5.0, c, 1.0] for i, c in enumerate(_BULLISH_CLOSES)]
# A genuinely flat series - fast/slow EMA both settle at the same value,
# so `crossed_up`/`crossed_down` (strict `>`/`<` on the current bar) are
# never true. Used wherever a pair must have NO actionable signal.
FLAT_OHLCV = [[i, 100.0, 100.5, 99.5, 100.0, 1.0] for i in range(20)]


class FakeNotifier(Notifier):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, target: str, message: str) -> None:
        self.sent.append((target, message))


class FakeClient(FakeBinance):
    """Thin adapter keeping this module's call sites unchanged: a
    stateful FakeBinance (tests/fake_binance.py) that serves OHLCV per
    pair and tracks every pair asked for and every order placed."""

    def __init__(self, *, market_type="spot", ohlcv_by_pair=None, free_balance=None, leverage_by_symbol=None, leverage_raises=False, **kw):
        super().__init__(
            market_type=market_type,
            ohlcv_by_pair=ohlcv_by_pair,
            free_balance=free_balance,
            leverage_by_symbol=leverage_by_symbol,
            leverage_raises=leverage_raises,
            price=next(iter((ohlcv_by_pair or {}).values()), FLAT_OHLCV)[-1][4],
            **kw,
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
    encryption_key: str,
    has_paid: bool = True,
    market_type: str = "spot",
    pair: str = "BTC/USDT",
    pair_selection_mode: str = "manual",
    trial_expired: bool = True,
    live_trading_enabled: bool = False,
    bot_started: bool = False,
    allocated_capital: float | None = None,
    credential_environment: str | None = "mainnet",
) -> str:
    customer = Customer(email=email, password_hash="x", has_paid=has_paid)
    session.add(customer)
    session.flush()

    now = datetime.now(timezone.utc)
    account = TradingAccount(
        customer_id=customer.id,
        bot_code=f"MBA-{abs(hash(email)) % 999999:06d}",
        pair=pair,
        market_type=market_type,
        pair_selection_mode=pair_selection_mode,
        notification_channel="telegram",
        notification_target="555000111",
        trial_ends_at=start_trial(now=now - timedelta(days=30), trial_days=7)
        if trial_expired
        else start_trial(now=now, trial_days=7),
        live_trading_enabled_at=now if live_trading_enabled else None,
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
                encrypted_api_key=encrypt(f"synthetic-{email}-key", key=encryption_key),
                encrypted_api_secret=encrypt(f"synthetic-{email}-secret", key=encryption_key),
            )
        )
    return account.id


def _run_cycle(account_id: str, settings: Settings) -> FakeNotifier:
    notifier = FakeNotifier()

    async def go():
        with session_scope() as session:
            account = session.get(TradingAccount, account_id)
            await process_account(session, account, settings, notifier)

    asyncio.run(go())
    return notifier


# --- Trading Capital: fail closed, and effective_capital = min(allocated, live balance) ---


def test_live_account_with_no_allocated_capital_places_no_order(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    client = FakeClient(ohlcv_by_pair={"BTC/USDT": BULLISH_OHLCV})
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="nocapital@example.com",
            encryption_key=settings.encryption_key,
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=None,  # never configured
        )

    _run_cycle(account_id, settings)

    assert client.market_orders == [], "an account with no allocated_capital must never place a live order"
    with session_scope() as session:
        assert session.query(Trade).filter(Trade.account_id == account_id).count() == 0


def test_effective_capital_is_the_smaller_of_allocated_and_live_balance(tmp_path, monkeypatch):
    """Customer allocated far more than Binance actually shows as free -
    the order must size off the real, smaller, live balance, never the
    customer's own configured ceiling."""
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    client = FakeClient(ohlcv_by_pair={"BTC/USDT": BULLISH_OHLCV}, free_balance={"USDT": 50.0})
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="smallbalance@example.com",
            encryption_key=settings.encryption_key,
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=100_000.0,  # far more than the 50.0 Binance actually shows
        )

    _run_cycle(account_id, settings)

    assert len(client.market_orders) == 1
    _, _, quantity = client.market_orders[0]
    entry_price = BULLISH_OHLCV[-1][4]
    # account_balance in compute_position_size was 50.0 (the live
    # balance), not 100,000 (what the customer configured) - the
    # resulting notional must be sane relative to 50, not 100,000.
    assert quantity * entry_price <= 50.0 * settings.max_notional_multiplier + 1e-6


def test_effective_capital_is_capped_by_the_customers_own_allocation_even_with_plenty_of_live_balance(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings(max_notional_multiplier=100.0)  # effectively disable the notional ceiling for this test
    client = FakeClient(ohlcv_by_pair={"BTC/USDT": BULLISH_OHLCV}, free_balance={"USDT": 1_000_000.0})
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="smallallocation@example.com",
            encryption_key=settings.encryption_key,
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=100.0,  # far less than the 1,000,000 Binance shows as free
        )

    _run_cycle(account_id, settings)

    with session_scope() as session:
        trade = session.query(Trade).filter(Trade.account_id == account_id).one()
        # risk_amount == effective_capital * risk_pct/100 == 100 * 1.0/100 == 1.0
        assert trade.quantity * abs(trade.entry_price - trade.stop_loss_price) <= 1.0 + 1e-6


# --- Exposure/margin ceilings actually bind through trader.py, not just risk.py in isolation ---


def test_exposure_ceiling_trims_an_oversized_live_order(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings(max_notional_multiplier=0.01)  # deliberately tiny, to force the ceiling to bind
    client = FakeClient(ohlcv_by_pair={"BTC/USDT": BULLISH_OHLCV}, free_balance={"USDT": 50_000.0})
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="ceiling@example.com",
            encryption_key=settings.encryption_key,
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=1_000.0,
        )

    _run_cycle(account_id, settings)

    assert len(client.market_orders) == 1
    _, _, quantity = client.market_orders[0]
    entry_price = BULLISH_OHLCV[-1][4]
    assert quantity > 0
    assert quantity * entry_price <= 1_000.0 * 0.01 + 1e-6


# The shared BULLISH_OHLCV puts the stop ~52% from entry, so a 1% risk budget sizes a position far BELOW any margin
# ceiling and the ceiling can never bind. Shifting every price up by 10,000 keeps the EMA crossover and the ATR (23.5)
# identical but makes the stop 47 away from a ~10,090 entry (0.47%): the risk budget (1% of 1,000 = 10) then asks for
# a ~2,139 notional, which is above 1x and 2x of the 1,000 allocated and below 10x, so the ceiling binds exactly where
# it should and nowhere else.
_TIGHT_STOP_OHLCV = [[t, o + 10_000.0, h + 10_000.0, l + 10_000.0, c + 10_000.0, v] for t, o, h, l, c, v in BULLISH_OHLCV]


@pytest.mark.parametrize("leverage, expected", [(1.0, "capped"), (2.0, "capped"), (10.0, "uncapped")])
def test_margin_ceiling_uses_the_real_read_only_leverage_on_futures(tmp_path, monkeypatch, leverage, expected):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings(max_notional_multiplier=100.0)  # isolate the margin ceiling from the notional one
    client = FakeClient(market_type="futures",
        ohlcv_by_pair={"BTC/USDT": _TIGHT_STOP_OHLCV}, free_balance={"USDT": 50_000.0}, leverage_by_symbol={"BTC/USDT": leverage}
    )
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="margin@example.com",
            encryption_key=settings.encryption_key,
            market_type="futures",
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=1_000.0,
        )

    _run_cycle(account_id, settings)

    assert client.fetch_leverage_calls == ["BTC/USDT:USDT"]  # the ccxt USD-M symbol; read, never set_leverage (no such method exists on FakeClient)
    assert len(client.market_orders) == 1
    _, _, quantity = client.market_orders[0]
    entry_price = _TIGHT_STOP_OHLCV[-1][4]
    notional = quantity * entry_price
    one_step = entry_price * 0.001  # the fake's quantity step
    risk_sized = 10.0 / 47.0 * entry_price  # 1% of 1,000 over a 47 stop distance, before any ceiling
    ceiling = 1_000.0 * leverage  # margin required (notional / leverage) must not exceed the 1,000 allocated
    if expected == "capped":
        assert notional <= ceiling + 1e-6, "required margin must never exceed the allocated capital"
        assert notional >= ceiling - one_step - 1e-6, "and the ceiling must be what limited it, not something else"
        assert notional < risk_sized - one_step, "the risk budget alone would have asked for more"
    else:
        assert notional == pytest.approx(risk_sized, abs=one_step + 1e-6), "a high enough leverage leaves the risk-sized quantity alone"
        assert notional > 2_000.0 and notional <= ceiling


def test_spot_account_never_queries_leverage_at_all(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    client = FakeClient(ohlcv_by_pair={"BTC/USDT": BULLISH_OHLCV}, free_balance={"USDT": 50_000.0})
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="spotnoleverage@example.com",
            encryption_key=settings.encryption_key,
            market_type="spot",
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=1_000.0,
        )

    _run_cycle(account_id, settings)

    assert client.fetch_leverage_calls == []
    assert len(client.market_orders) == 1, "Spot entries must be entirely unaffected by the Futures leverage fail-closed rule"


# --- Futures fail-closed: an unverifiable leverage/margin check must never ---
# --- be silently skipped for a new live Futures entry ---


def test_futures_entry_proceeds_when_leverage_is_valid(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings(max_notional_multiplier=100.0)
    client = FakeClient(market_type="futures", 
        ohlcv_by_pair={"BTC/USDT": BULLISH_OHLCV}, free_balance={"USDT": 50_000.0}, leverage_by_symbol={"BTC/USDT": 1.0}
    )
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="futuresvalidleverage@example.com",
            encryption_key=settings.encryption_key,
            market_type="futures",
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=1_000.0,
        )

    _run_cycle(account_id, settings)

    assert len(client.market_orders) == 1, "a valid, correctly-constrained Futures entry must proceed"
    _, _, quantity = client.market_orders[0]
    entry_price = BULLISH_OHLCV[-1][4]
    assert (quantity * entry_price) / 2.0 <= 1_000.0 + 1e-6

    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        assert account.last_error is None


def test_futures_entry_fails_closed_when_leverage_read_raises(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    client = FakeClient(market_type="futures", ohlcv_by_pair={"BTC/USDT": BULLISH_OHLCV}, free_balance={"USDT": 50_000.0}, leverage_raises=True)
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="futuresleverageraises@example.com",
            encryption_key=settings.encryption_key,
            market_type="futures",
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=1_000.0,
        )

    _run_cycle(account_id, settings)

    assert client.market_orders == [], "a Mainnet Futures entry must never proceed when the leverage read itself fails"
    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        assert account.last_error is not None
        assert session.query(Trade).filter(Trade.account_id == account_id).count() == 0


def test_futures_entry_fails_closed_when_leverage_is_missing(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    client = FakeClient(market_type="futures", ohlcv_by_pair={"BTC/USDT": BULLISH_OHLCV}, free_balance={"USDT": 50_000.0}, leverage_by_symbol={})

    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="futuresleveragemissing@example.com",
            encryption_key=settings.encryption_key,
            market_type="futures",
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=1_000.0,
        )

    _run_cycle(account_id, settings)

    assert client.market_orders == [], "a Mainnet Futures entry must never proceed when leverage can't be found at all"
    with session_scope() as session:
        assert session.query(Trade).filter(Trade.account_id == account_id).count() == 0


def test_futures_entry_fails_closed_when_leverage_is_zero(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    client = FakeClient(market_type="futures", 
        ohlcv_by_pair={"BTC/USDT": BULLISH_OHLCV}, free_balance={"USDT": 50_000.0}, leverage_by_symbol={"BTC/USDT": 0}
    )
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="futuresleveragezero@example.com",
            encryption_key=settings.encryption_key,
            market_type="futures",
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=1_000.0,
        )

    _run_cycle(account_id, settings)

    assert client.market_orders == [], "a Mainnet Futures entry must never proceed with a non-positive leverage value"
    with session_scope() as session:
        assert session.query(Trade).filter(Trade.account_id == account_id).count() == 0


def test_futures_leverage_read_failure_never_blocks_managing_an_already_open_position(tmp_path, monkeypatch):
    """The fail-closed rule is about NEW entries only - an already-open
    Futures position must keep being monitored/exited for real even if
    the leverage read is completely broken, since _maybe_exit never
    reads or needs leverage at all."""
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    client = FakeClient(market_type="futures", ohlcv_by_pair={"BTC/USDT": FLAT_OHLCV}, leverage_raises=True)
    client.position = 0.01  # the real Binance position this legacy row describes
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="futuresopenposition@example.com",
            encryption_key=settings.encryption_key,
            market_type="futures",
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=1_000.0,
        )
        session.add(
            Trade(
                account_id=account_id,
                side="long",
                is_paper=False,
                entry_price=90.0,
                quantity=0.01,
                stop_loss_price=999_999.0,  # trivially hit
                status="open",
                opened_at=datetime.now(timezone.utc),
            )
        )

    _run_cycle(account_id, settings)

    assert client.fetch_leverage_calls == [], "exiting an open position never needs a leverage read in the first place"
    assert len(client.market_orders) == 1, "the open Futures position's real closing order must still be placed"
    with session_scope() as session:
        trade = session.query(Trade).filter(Trade.account_id == account_id).one()
        assert trade.status == "closed"


def test_futures_notional_ceiling_still_enforced_independently_of_the_margin_ceiling(tmp_path, monkeypatch):
    """Even with perfectly valid leverage, the separate notional ceiling
    must still bind on its own - the margin fail-closed rule is an
    ADDITIONAL check, never a replacement for it. (1x: the synthetic stop is ~52% from entry, which the
    pre-entry protection guard, enforced in this file's settings, only accepts at very low leverage.)"""
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings(max_notional_multiplier=0.01)  # deliberately tiny
    client = FakeClient(market_type="futures", 
        ohlcv_by_pair={"BTC/USDT": BULLISH_OHLCV}, free_balance={"USDT": 50_000.0}, leverage_by_symbol={"BTC/USDT": 1.0}
    )
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="futuresnotionalstillbinds@example.com",
            encryption_key=settings.encryption_key,
            market_type="futures",
            live_trading_enabled=True,
            bot_started=True,
            allocated_capital=1_000.0,
        )

    _run_cycle(account_id, settings)

    assert len(client.market_orders) == 1
    _, _, quantity = client.market_orders[0]
    entry_price = BULLISH_OHLCV[-1][4]
    assert quantity * entry_price <= 1_000.0 * 0.01 + 1e-6


# --- The critical migration-safety fix: an already-open LIVE trade must ---
# --- never be silently managed against testnet credentials/market data ---


def test_open_live_trade_keeps_being_managed_with_real_credentials_even_if_the_account_is_newly_forced_to_paper(
    tmp_path, monkeypatch
):
    """Simulates exactly the dangerous migration scenario: an account
    that was already fully live-authorized (paid, mainnet credential,
    live trading enabled) and already has an OPEN real trade, but
    `bot_started_at` is None - the default every pre-existing account
    gets from the new migration, since no Start consent is ever
    fabricated. `resolve_trading_mode` now returns PAPER for this
    account's *new entries*, but the already-open trade must still be
    closed for real: real credentials, real exchange environment, and
    (once its trivially-hit stop fires) a real `create_market_order`
    call - never silently downgraded to testnet because of a gate that
    only governs whether a NEW position may open."""
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    captured_kwargs = {}
    client = FakeClient(ohlcv_by_pair={"BTC/USDT": FLAT_OHLCV})
    client.free["BTC"] = 0.01  # the real Binance holding this legacy row describes

    def fake_builder(**kwargs):
        captured_kwargs.update(kwargs)
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", fake_builder)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="legacy-live-open-trade@example.com",
            encryption_key=settings.encryption_key,
            live_trading_enabled=True,
            bot_started=False,  # the migration default - no Start consent fabricated
            allocated_capital=None,  # also never configured pre-migration
        )
        # A trivially-hit stop (way above the flat series' ~100 price for
        # a long) means this trade's exit fires on the very first cycle
        # regardless of any crossover signal - isolating the test to the
        # one thing that matters: was it closed for real.
        session.add(
            Trade(
                account_id=account_id,
                side="long",
                is_paper=False,
                entry_price=90.0,
                quantity=0.01,
                stop_loss_price=999_999.0,
                status="open",
                opened_at=datetime.now(timezone.utc),
            )
        )

    _run_cycle(account_id, settings)

    assert captured_kwargs["sandbox"] is False, "an open live trade must be managed against the real exchange, never sandbox"
    assert captured_kwargs["api_key"] == "synthetic-legacy-live-open-trade@example.com-key", (
        "an open live trade must be managed with the account's OWN decrypted credential, never the shared platform testnet key"
    )
    assert len(client.market_orders) == 1, "the open live trade's real closing order was never placed"

    with session_scope() as session:
        trade = session.query(Trade).filter(Trade.account_id == account_id).one()
        assert trade.status == "closed"
        account = session.get(TradingAccount, account_id)
        # The account's own bot_started_at is still None - the fix does
        # not fabricate Start consent either, it only keeps the ALREADY
        # OPEN position properly managed.
        assert account.bot_started_at is None


def test_same_forced_to_paper_account_with_no_open_trade_uses_testnet_for_its_scan(tmp_path, monkeypatch):
    """Control for the test above: with no open position, the same
    not-yet-started account's scan correctly uses the platform TESTNET
    path (current behavior for any PAPER-mode cycle) - proving the fix
    is scoped to protecting an open position, not a blanket switch to
    live credentials for every not-started account."""
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    captured_kwargs = {}
    client = FakeClient(ohlcv_by_pair={"BTC/USDT": FLAT_OHLCV})

    def fake_builder(**kwargs):
        captured_kwargs.update(kwargs)
        return client

    monkeypatch.setattr(exchange, "build_exchange_client", fake_builder)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="legacy-live-no-trade@example.com",
            encryption_key=settings.encryption_key,
            live_trading_enabled=True,
            bot_started=False,
            allocated_capital=None,
        )

    _run_cycle(account_id, settings)

    assert captured_kwargs["sandbox"] is True
    assert captured_kwargs["api_key"] == settings.testnet_spot_api_key


# --- db.list_active_accounts: the kill switch must never orphan an open position ---


def test_list_active_accounts_always_includes_an_inactive_account_with_an_open_trade(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    with session_scope() as session:
        inactive_with_trade = _make_account(session, email="inactive-open@example.com", encryption_key=settings.encryption_key)
        inactive_no_trade = _make_account(session, email="inactive-flat@example.com", encryption_key=settings.encryption_key)
        active_no_trade = _make_account(session, email="active-flat@example.com", encryption_key=settings.encryption_key)

        session.get(TradingAccount, inactive_with_trade).is_active = False
        session.get(TradingAccount, inactive_no_trade).is_active = False
        session.add(
            Trade(
                account_id=inactive_with_trade,
                side="long",
                is_paper=True,
                entry_price=100.0,
                quantity=1.0,
                stop_loss_price=90.0,
                status="open",
                opened_at=datetime.now(timezone.utc),
            )
        )

    with session_scope() as session:
        ids = {a.id for a in list_active_accounts(session)}

    assert inactive_with_trade in ids, "an open position must never be orphaned by the internal kill switch"
    assert inactive_no_trade not in ids
    assert active_no_trade in ids


# --- Auto Select: never switches pairs while a position is open, and only ---
# --- ever picks from the approved universe when genuinely flat ---


def test_auto_select_never_reassigns_pair_while_a_trade_is_open(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    # Every OTHER pair in the universe has a real actionable signal -
    # if the scan ran at all, it would have every reason to switch.
    ohlcv_by_pair = {pair: BULLISH_OHLCV for pair in SUPPORTED_PAIRS}
    ohlcv_by_pair["BTC/USDT"] = FLAT_OHLCV
    client = FakeClient(ohlcv_by_pair=ohlcv_by_pair)
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="autoselect-open@example.com",
            encryption_key=settings.encryption_key,
            has_paid=False,
            pair="BTC/USDT",
            pair_selection_mode="auto",
        )
        session.add(
            Trade(
                account_id=account_id,
                side="long",
                is_paper=True,
                entry_price=100.0,
                quantity=1.0,
                stop_loss_price=999_999.0,  # trivially hit, so the cycle definitely reaches _maybe_exit
                status="open",
                opened_at=datetime.now(timezone.utc),
            )
        )

    _run_cycle(account_id, settings)

    assert client.fetched_pairs == ["BTC/USDT"], "the scan must never run at all while a position is open"
    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        assert account.pair == "BTC/USDT"


def test_auto_select_picks_the_best_actionable_pair_when_flat(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    ohlcv_by_pair = {pair: FLAT_OHLCV for pair in SUPPORTED_PAIRS}
    ohlcv_by_pair["ETH/USDT"] = BULLISH_OHLCV  # the one candidate with an actionable signal
    client = FakeClient(ohlcv_by_pair=ohlcv_by_pair)
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="autoselect-flat@example.com",
            encryption_key=settings.encryption_key,
            has_paid=False,
            pair="BTC/USDT",
            pair_selection_mode="auto",
        )

    _run_cycle(account_id, settings)

    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        assert account.pair == "ETH/USDT"
        assert account.last_signal == "bullish_crossover"
        trade = session.query(Trade).filter(Trade.account_id == account_id).one()
        assert trade.side == "long"


def test_auto_select_leaves_pair_unchanged_when_nothing_in_the_universe_has_a_signal(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path}/test.db")
    settings = _settings()
    client = FakeClient(ohlcv_by_pair={pair: FLAT_OHLCV for pair in SUPPORTED_PAIRS})
    monkeypatch.setattr(exchange, "build_exchange_client", lambda **kwargs: client)

    with session_scope() as session:
        account_id = _make_account(
            session,
            email="autoselect-nothing@example.com",
            encryption_key=settings.encryption_key,
            has_paid=False,
            pair="BTC/USDT",
            pair_selection_mode="auto",
        )

    _run_cycle(account_id, settings)

    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        assert account.pair == "BTC/USDT"
        assert account.last_signal == "none"
        assert account.last_error is None  # a quiet scan is healthy, not an error
        assert session.query(Trade).filter(Trade.account_id == account_id).count() == 0
