"""An account with no linked Telegram chat is skipped entirely.

The customer-facing screens (the link banner, the status line, the how-to) say the bot does not scan or
trade until Telegram is linked. The code that does it is the first lines of trader.process_account. This
test proves it with the fake exchange: no market data read, no order, no trade, no scan time recorded.
Control flow only, not Binance behaviour.
"""
from __future__ import annotations

import pytest
from helpers_live import *  # noqa: F401,F403

from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade, TradingAccount


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _state(account_id):
    with session_scope() as session:
        account = session.get(TradingAccount, account_id)
        trades = session.query(Trade).filter(Trade.account_id == account_id).count()
        return account.last_scanned_at, trades


def test_an_account_without_a_linked_chat_is_not_scanned_and_places_nothing(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="spot", notification_target=None)
    notifier = run_cycle(account_id, settings)
    scanned, trades = _state(account_id)
    assert scanned is None  # the cycle never started for this account
    assert trades == 0
    assert client.orders == {}  # no order of any kind
    assert client.calls == []  # not even a market data read
    assert notifier.sent == []


def test_the_same_account_is_scanned_once_the_chat_is_linked(db, monkeypatch):
    settings = make_settings(max_notional_multiplier=100.0)
    client = spot_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="spot", notification_target="555000111")
    run_cycle(account_id, settings)
    scanned, _trades = _state(account_id)
    assert scanned is not None and client.calls  # the control: linking is the only difference
