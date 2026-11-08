"""Profit wording (PNL_LABELS, default OFF): with the switch off every message and API response is what it always was; with it on,
every profit figure says "before fees" (live) or "simulated, no fees or slippage" (paper). No number changes either way.

`trades.pnl` is gross: (exit - entry) x quantity, no fees, no funding. These tests prove the WORDS and that numbers are untouched; they
say nothing about what a trade really cost on Binance."""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import ccxt.async_support as ccxt
import pytest
from fake_binance import FakeBinance
from helpers_live import *  # noqa: F401,F403

from mba_bot import pnl_labels, scheduler
from mba_bot.config import load_settings
from mba_bot.db import init_db, session_scope
from mba_bot.models import Trade

ROOT = Path(__file__).parent.parent
FRONT = ROOT / "frontend" / "src"


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


# ------------------------------------------------------------------ the strings


def test_off_is_exactly_the_old_wording():
    assert pnl_labels.exit_line(100.0, 101.5, 1.5, on=False, quote="USDT") == "Entry: 100.0000  Exit: 101.5000  P&L: +1.50"
    assert pnl_labels.exit_line(100.0, 98.0, -2.0, on=False, paper=True) == "Entry: 100.0000  Exit: 98.0000  P&L: -2.00"
    assert pnl_labels.daily_summary("2026-11-08", 3, 4.25, "USDT", on=False) == "Daily P&L summary (2026-11-08 UTC): 3 trade(s) closed, net +4.25 USDT"
    assert pnl_labels.basis() is None


def test_on_names_the_basis_and_the_currency(monkeypatch):
    assert pnl_labels.exit_line(100.0, 101.5, 1.5, on=True, quote="USDT").endswith("P&L before fees: +1.50 USDT")
    assert pnl_labels.exit_line(100.0, 98.0, -2.0, on=True, paper=True, quote="USDT").endswith("P&L (simulated, no fees or slippage): -2.00 USDT")
    assert pnl_labels.exit_line(1.0, 2.0, 1.0, on=True).endswith("P&L before fees: +1.00")  # no quote known: no unit invented
    live = pnl_labels.daily_summary("2026-11-08", 3, 4.25, "USDT", on=True, paper_count=0)
    paper = pnl_labels.daily_summary("2026-11-08", 3, 4.25, "USDT", on=True, paper_count=3)
    mixed = pnl_labels.daily_summary("2026-11-08", 3, 4.25, "USDT", on=True, paper_count=1)
    assert "before fees" in live and "simulated" not in live and "net" not in live
    assert "simulated, no fees or slippage" in paper and "before fees" not in paper
    assert "before fees" in mixed and "practice trades are simulated" in mixed
    for text in (live, paper, mixed):
        assert "Daily P&L summary" in text and "3 trade(s) closed" in text and "+4.25 USDT" in text  # what the existing tests look for
    monkeypatch.setenv("PNL_LABELS", "1")
    assert pnl_labels.enabled() and pnl_labels.basis() == "gross_before_fees"
    monkeypatch.setenv("PNL_LABELS", "off")
    assert not pnl_labels.enabled()


def test_the_setting_defaults_off_and_follows_the_environment(monkeypatch):
    monkeypatch.delenv("PNL_LABELS", raising=False)
    assert load_settings().pnl_labels is False
    monkeypatch.setenv("PNL_LABELS", "true")
    assert load_settings().pnl_labels is True


def test_no_raw_profit_message_is_left_in_the_trading_code():
    """Every profit line in a message goes through pnl_labels, so a new message cannot forget the wording."""
    for name in ("lifecycle.py", "trader.py"):
        source = (ROOT / "src" / "mba_bot" / name).read_text()
        code = "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("#"))
        assert "P&L:" not in code and "net {" not in code, name
        assert "pnl_labels." in code, name


# ------------------------------------------------------------------ end to end: the live exit messages


def _pnl_line(notifier, prefix="[LIVE]"):
    lines = [t for t in notifier.texts if t.startswith(prefix) and "P&L" in t]
    assert lines, notifier.texts
    return lines[-1]


@pytest.mark.parametrize("on", [False, True])
def test_strategy_exit_message(db, monkeypatch, on):
    settings = make_settings(max_notional_multiplier=100.0, pnl_labels=on)
    client, settings, account_id = open_protected(monkeypatch, market="spot", settings=settings)
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    notifier = run_cycle(account_id, settings)
    trade = the_trade(account_id)
    line = _pnl_line(notifier)
    assert f"{trade.pnl:+.2f}" in line  # the number is the recorded gross pnl either way
    if on:
        assert "P&L before fees:" in line and line.rstrip().endswith("USDT")
    else:
        assert re.search(r"P&L: [+-]\d", line) and "before fees" not in line


@pytest.mark.parametrize("on", [False, True])
def test_exchange_stop_exit_message(db, monkeypatch, on):
    settings = make_settings(max_notional_multiplier=100.0, pnl_labels=on)
    client, settings, account_id = open_protected(monkeypatch, market="futures", settings=settings)
    client.move_price(40.0)
    notifier = run_cycle(account_id, settings)
    line = _pnl_line(notifier)
    assert ("P&L before fees:" in line) is on and (re.search(r"P&L: [+-]\d", line) is not None) is (not on)


@pytest.mark.parametrize("on", [False, True])
def test_safety_exit_message(db, monkeypatch, on):
    settings = make_settings(max_notional_multiplier=100.0, pnl_labels=on)
    client = futures_client()
    client.fail("create_order", role="stop", exc=ccxt.InvalidOrder("rejected"), times=99)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    notifier = run_cycle(account_id, settings)
    line = _pnl_line(notifier, "[LIVE] Safety exit")
    assert ("P&L before fees:" in line) is on and (re.search(r"P&L: [+-]\d", line) is not None) is (not on)


@pytest.mark.parametrize("on", [False, True])
def test_paper_exit_and_daily_summary_messages(db, monkeypatch, on):
    settings = make_settings(max_notional_multiplier=100.0, pnl_labels=on, testnet_spot_api_key="k", testnet_spot_api_secret="s")
    client = spot_client(ohlcv=FLAT_OHLCV)
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="spot")
    with session_scope() as session:  # make it a paper account with a paper trade that the stop closes this cycle
        from mba_bot.models import TradingAccount

        acct = session.get(TradingAccount, account_id)
        acct.live_trading_enabled_at = None
        acct.bot_started_at = None
        session.add(Trade(account_id=account_id, side="long", is_paper=True, entry_price=100.0, quantity=1.0, stop_loss_price=10_000.0,
                          status="open", pair="BTC/USDT"))
    notifier = run_cycle(account_id, settings)
    exit_line = [t for t in notifier.texts if t.startswith("[PAPER] Exited")][0]
    summary = [t for t in notifier.texts if "Daily P&L summary" in t][0]
    assert ("P&L (simulated, no fees or slippage):" in exit_line) is on
    assert ("simulated, no fees or slippage" in summary) is on and ("net " in summary) is (not on)
    assert "1 trade(s) closed" in summary


# ------------------------------------------------------------------ the API


def _signup_with_account(client, email):
    client.post("/api/auth/signup", json={"email": email, "password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True})
    created = client.post("/api/account", json={"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "api_key": "k", "api_secret": "s"})
    assert created.status_code == 201, created.text


@pytest.mark.parametrize("on", [False, True])
def test_the_api_adds_one_optional_field_and_changes_no_number(monkeypatch, on):
    import os
    import tempfile

    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/labels_api.db")
    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
    os.environ.setdefault("COOKIE_SECURE", "false")
    os.environ.setdefault("POLL_INTERVAL_SECONDS", "3600")
    from mba_bot.api.app import app

    if on:
        monkeypatch.setenv("PNL_LABELS", "1")
    else:
        monkeypatch.delenv("PNL_LABELS", raising=False)
    email = f"labels-{'on' if on else 'off'}@example.com"
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, email)
        got = client.get("/api/account")
        summary = client.get("/api/trades/summary", params={"is_paper": "true"})
    assert got.status_code == 200 and summary.status_code == 200
    expected = "gross_before_fees" if on else None
    assert got.json()["pnl_basis"] == expected and summary.json()["pnl_basis"] == expected
    assert summary.json()["net_pnl"] == 0.0 and summary.json()["pnl_today"] == 0.0 and summary.json()["total_trades"] == 0  # names and values untouched


# ------------------------------------------------------------------ the frontend wording (pure module, run with node)


NODE = shutil.which("node")


def _labels(account, paper):
    code = f"import {{ pnlLabels }} from {json.dumps(str(FRONT / 'pnlLabels.js'))}; const L = pnlLabels({json.dumps(account)}, {{ paper: {str(paper).lower()} }}); " \
           "console.log(JSON.stringify({...L, windowPnl: L.windowPnl('30D'), glossary: L.glossary('BASE')}));"
    out = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(not NODE, reason="node is needed to run the frontend wording module")
def test_frontend_wording_off_is_the_old_text_and_on_names_the_basis():
    off = _labels({"pair": "BTC/USDT", "pnl_basis": None}, False)
    assert off["on"] is False and off["allTime"] == "All-Time P&L" and off["testPnl"] == "Test P&L" and off["winRate"] == "Win Rate"
    assert off["windowPnl"] == "P&L (30D)" and off["cumulative"] == "Cumulative P&L" and off["caption"] is None and off["unit"] == "" and off["glossary"] == "BASE"
    assert _labels(None, False)["on"] is False  # no account yet (loading): old wording
    live = _labels({"pair": "BTC/USDT", "pnl_basis": "gross_before_fees"}, False)
    assert live["on"] and "before fees" in live["allTime"] and "before fees" in live["winRate"] and "before fees" in live["windowPnl"]
    assert "before fees" in live["cumulative"] and "Binance balance" in live["caption"] and live["unit"] == "USDT" and live["rowNote"] == "before fees"
    assert "not included" in live["glossary"] and live["glossary"].startswith("BASE")
    paper = _labels({"pair": "ETH/USDT", "pnl_basis": "gross_before_fees"}, True)
    assert "simulated" in paper["allTime"] and "simulated" in paper["testPnl"] and "no fees" in paper["testPnl"] and paper["rowNote"] == "simulated"


def test_every_profit_label_on_the_pages_comes_from_the_wording_module():
    pages = ["pages/dashboard/LiveDashboard.jsx", "pages/dashboard/TestnetDashboard.jsx", "pages/Performance.jsx", "pages/TradeHistory.jsx", "pages/Positions.jsx", "pages/Help.jsx"]
    for rel in pages:
        text = (FRONT / rel).read_text()
        assert "pnlLabels" in text, rel
        for raw in ('label="All-Time P&L"', 'label="Test P&L"', 'label="Win Rate"', "label={`P&L (", "Cumulative P&L</h2>"):
            assert raw not in text, f"{rel} still hard-codes {raw}"
    ui = (FRONT / "components" / "ui.jsx").read_text()
    assert "unit" in ui.split("export function PnlText")[1].split("export function EmptyState")[0]
