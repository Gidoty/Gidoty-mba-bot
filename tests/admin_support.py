"""Shared helpers for the admin dashboard tests: an isolated database, an app with the admin routes mounted, signed-in clients, and seed functions. Not a test file."""
from __future__ import annotations

import dataclasses
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from mba_bot import admin_auth, admin_service, db as dbmod
from mba_bot.api import routers as _routers  # noqa: F401
from mba_bot.api.ratelimit import SlidingWindowLimiter
from mba_bot.api.security import SESSION_COOKIE_NAME, create_session_token
from mba_bot.config import load_settings
from mba_bot.models import ApiCredential, Customer, Trade, TradingAccount

ADMIN_EMAIL = "owner@example.com"
OTHER_ADMIN_EMAIL = "second.admin@example.com"
NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)


def make_env(monkeypatch, tmp_path, *, flag="1", admins=ADMIN_EMAIL):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/admin.db")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-for-admin")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "mba_test_bot")
    monkeypatch.setenv("ADMIN_DASHBOARD_ENABLED", flag)
    monkeypatch.setenv("ADMIN_EMAILS", admins)
    monkeypatch.delenv("DEPLOY_COMMIT", raising=False)
    admin_service.reset_cache()
    from mba_bot.api.routers import admin as admin_router  # noqa: WPS433 - only importable meaningfully with the flag on, but harmless to import

    admin_router._login_limiter = SlidingWindowLimiter()
    s = load_settings()
    dbmod.init_db(s.database_url)
    from mba_bot.api.app import create_app

    return create_app(), s


def client_for(app, customer_id: str, settings, *, admin: bool = False, headers=None) -> TestClient:
    c = TestClient(app, base_url="https://testserver", headers=headers or {})
    c.cookies.set(SESSION_COOKIE_NAME, create_session_token(customer_id=customer_id, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm, expires_minutes=60), path="/")
    if admin:
        c.cookies.set(admin_auth.ADMIN_COOKIE_NAME, admin_auth.create_admin_token(customer_id=customer_id, jwt_secret=settings.jwt_secret, algorithm=settings.jwt_algorithm), path="/api/admin")
    return c


_n = iter(range(1, 10_000_000))


def mk_customer(s, email=None, *, paid=False, active=True, created=None) -> Customer:
    c = Customer(email=email or f"person{next(_n)}@example.org", password_hash="$2b$12$CANARYPASSWORDHASHCANARYPASSWORDHASHCANARYPASSWORDHASH", has_paid=paid, is_active=active,
                 paystack_reference="PSK-CANARY-REF", created_at=created or NOW)
    s.add(c)
    s.flush()
    return c


def mk_account(s, cust, *, pair="BTC/USDT", market="spot", active=True, trial_over=True, bypass=False, live_enabled=False, started=False, mainnet=False, scanned=None, error=None, code=None, strategies=None) -> TradingAccount:
    a = TradingAccount(
        customer_id=cust.id, pair=pair, market_type=market, notification_channel="telegram", notification_target="CANARY-CHAT-ID-424242", is_active=active,
        trial_ends_at=NOW - timedelta(days=1) if trial_over else NOW + timedelta(days=30), live_bypass_accepted_at=NOW if bypass else None,
        live_trading_enabled_at=NOW if live_enabled else None, bot_started_at=NOW if started else None, last_scanned_at=scanned, last_error=error, last_error_at=NOW if error else None,
        bot_code=code, telegram_link_token=f"CANARY-LINK-TOKEN-{next(_n)}",
    )
    s.add(a)
    s.flush()
    if mainnet is not None:
        s.add(ApiCredential(account_id=a.id, encrypted_api_key="gAAAA-CANARY-ENCRYPTED-KEY", encrypted_api_secret="gAAAA-CANARY-ENCRYPTED-SECRET", environment="mainnet" if mainnet else "testnet"))
        s.flush()
    return a


def mk_trade(s, acct, *, status="closed", paper=True, demo=False, pnl=1.0, strategy=None, fees=None, closed=None, pair="BTC/USDT", protection=None, slot=0) -> Trade:
    t = Trade(account_id=acct.id, side="long", is_paper=paper, is_demo=demo, entry_price=100.0, quantity=1.0, stop_loss_price=95.0, status=status, pnl=pnl if status == "closed" else None,
              strategy_id=strategy, pair=pair, closed_at=closed or NOW - timedelta(days=1) if status == "closed" else None, protection_status=protection, live_slot=slot)
    if fees is not None:
        t.entry_fee_quote, t.exit_fee_quote = fees
    s.add(t)
    s.flush()
    return t
