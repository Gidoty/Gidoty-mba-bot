"""B2: per-account reporting (REPORTING_V2, default off).

PROVES: closed trades are grouped by mode (live, demo, practice), strategy and pair and the three modes are never added together; gross is the sum of the stored result, fees the sum of the real captured
commissions, and a NET figure exists only for a live or demo group in which EVERY trade has both fee numbers (practice is always simulated and never net; a group with a missing fee is "before fees");
open trades and trades with no readable result are not counted; the daily Telegram summary keeps the modes apart and uses the word net only when every fee of that mode's trades is known, and with the
switch off is the old text; the API is read-only, off by default (409), shows a customer only their own account, takes a window, and carries no key, secret, e-mail or account id; an end-to-end Futures trade
closed through the lifecycle reports net = gross minus the fees captured from the fills.
DOES NOT PROVE: any real Binance fee (the fake exchange sets its own), Futures funding (never included), or the screen in a browser."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_reporting.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402
from helpers_live import BEAR_PRICE, BEARISH_OHLCV, BULLISH_OHLCV, FakeNotifier, futures_client, install, make_settings, new_account, run_cycle, the_trade  # noqa: E402
from test_api_live_trading_safety import _signup_with_account  # noqa: E402

from mba_bot import reporting as rp, trader  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.deps import get_settings  # noqa: E402
from mba_bot.config import load_settings  # noqa: E402
from mba_bot.db import init_db, session_scope  # noqa: E402
from mba_bot.models import Customer, Trade, TradingAccount  # noqa: E402


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")
    return tmp_path


def T(*, paper=False, demo=False, pnl=1.0, entry_fee=None, exit_fee=None, strategy=None, pair="BTC/USDT", status="closed", closed_at=None):
    return SimpleNamespace(is_paper=paper, is_demo=demo, pnl=pnl, entry_fee_quote=entry_fee, exit_fee_quote=exit_fee, strategy_id=strategy, pair=pair, status=status, closed_at=closed_at)


# ------------------------------------------------------------------ the grouping and the net rule
def test_modes_are_never_added_together_and_each_is_grouped_by_strategy_and_pair():
    trades = [T(pnl=2.0, entry_fee=0.1, exit_fee=0.1), T(pnl=-1.0, entry_fee=0.1, exit_fee=0.1, strategy="rsi_mr_v1", pair="ETH/USDT"), T(paper=True, pnl=50.0), T(demo=True, pnl=7.0, entry_fee=0.2, exit_fee=0.2)]
    r = rp.build(trades)
    assert [m["mode"] for m in r["modes"]] == ["live", "demo", "paper"]
    live, demo, paper = r["modes"]
    assert live["total"]["closed_trades"] == 2 and live["total"]["gross_pnl"] == 1.0 and demo["total"]["gross_pnl"] == 7.0 and paper["total"]["gross_pnl"] == 50.0  # three separate totals
    assert [(g["strategy"], g["closed_trades"]) for g in live["by_strategy"]] == [("ema_cross_atr_v1", 1), ("rsi_mr_v1", 1)]  # an empty strategy id is the EMA strategy
    assert sorted((g["strategy"], g["pair"]) for g in live["by_pair"]) == [("ema_cross_atr_v1", "BTC/USDT"), ("rsi_mr_v1", "ETH/USDT")]
    assert r["funding_note"].startswith("Futures funding")


def test_net_exists_only_when_every_trade_has_both_real_fee_numbers():
    complete = rp.build([T(pnl=2.0, entry_fee=0.1, exit_fee=0.2), T(pnl=1.0, entry_fee=0.1, exit_fee=0.1)])["modes"][0]["total"]
    assert complete["fees_complete"] is True and complete["gross_pnl"] == 3.0 and complete["fees"] == 0.5 and complete["net_pnl"] == 2.5
    for missing in (dict(entry_fee=0.1, exit_fee=None), dict(entry_fee=None, exit_fee=0.1), dict(entry_fee=None, exit_fee=None), dict(entry_fee=float("nan"), exit_fee=0.1)):
        partial = rp.build([T(pnl=2.0, entry_fee=0.1, exit_fee=0.2), T(pnl=1.0, **missing)])["modes"][0]["total"]
        assert partial["fees_complete"] is False and partial["net_pnl"] is None and partial["trades_with_fees"] == 1, missing  # one missing fee: no net for the group
    zero = rp.build([T(pnl=1.0, entry_fee=0.0, exit_fee=0.0)])["modes"][0]["total"]
    assert zero["fees_complete"] is True and zero["net_pnl"] == 1.0  # a captured fee of zero is a known fee


def test_a_practice_group_is_simulated_and_never_net_even_if_it_has_fee_numbers():
    paper = rp.build([T(paper=True, pnl=5.0, entry_fee=0.1, exit_fee=0.1)])["modes"][0]["total"]
    assert paper["simulated"] is True and paper["net_pnl"] is None and paper["fees_complete"] is False and paper["fees"] is None and paper["gross_pnl"] == 5.0


def test_open_trades_and_unreadable_results_are_not_counted():
    r = rp.build([T(pnl=1.0), T(status="open", pnl=100.0), T(status="void", pnl=100.0), T(pnl=None), T(pnl=float("nan"))])
    assert r["modes"][0]["total"]["closed_trades"] == 1 and r["modes"][0]["total"]["gross_pnl"] == 1.0
    assert rp.build([])["modes"] == []


def test_wins_and_losses_count_the_sign_of_the_stored_result():
    t = rp.build([T(pnl=1.0), T(pnl=-2.0), T(pnl=0.0)])["modes"][0]["total"]
    assert (t["wins"], t["losses"], t["closed_trades"]) == (1, 1, 3)


# ------------------------------------------------------------------ the daily summary
def test_the_daily_summary_keeps_the_modes_apart_and_says_net_only_with_every_fee_known():
    text = rp.daily_summary_text("2027-03-10", [T(pnl=2.0, entry_fee=0.1, exit_fee=0.1), T(demo=True, pnl=1.0), T(paper=True, pnl=9.0)], "USDT")
    lines = text.splitlines()
    assert lines[0] == "Daily summary (2027-03-10 UTC)"
    assert "Live: 1 trade closed, net +1.80 USDT after fees (+2.00 before fees, fees 0.20)" in lines
    assert "Demo (no real money): 1 trade closed, +1.00 USDT before fees" in lines  # no fees captured: not net
    assert "Practice: 1 trade closed, +9.00 USDT (simulated, no fees or slippage)" in lines
    partial = rp.daily_summary_text("2027-03-10", [T(pnl=2.0, entry_fee=0.1, exit_fee=0.1), T(pnl=1.0)], "USDT")
    assert "net" not in partial and "before fees, fees known for 1 of 2" in partial  # one missing fee: the word net is not used


def _closed_today_trades(aid):
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        for pnl, fee in ((2.0, 0.1), (-1.0, 0.1)):
            s.add(Trade(account_id=aid, side="long", is_paper=False, entry_price=100.0, quantity=1.0, stop_loss_price=95.0, status="closed", pair="BTC/USDT", exit_price=100.0 + pnl, pnl=pnl,
                        entry_fee_quote=fee, exit_fee_quote=fee, opened_at=now - timedelta(hours=2), closed_at=now - timedelta(minutes=5), close_reason="x"))


def test_the_telegram_summary_is_the_old_text_with_the_switch_off_and_the_new_one_with_it_on(db):
    aid = new_account(make_settings(), email="sum@example.com")
    _closed_today_trades(aid)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for on in (False, True):
        with session_scope() as s:
            s.get(TradingAccount, aid).last_pnl_summary_date = None
        n = FakeNotifier()

        async def go():
            with session_scope() as s:
                await trader._maybe_send_daily_summary(s, s.get(TradingAccount, aid), n, now=datetime.now(timezone.utc), settings=make_settings(reporting_v2=on))

        asyncio.run(go())
        text = n.texts[-1]
        if on:
            assert text.startswith(f"Daily summary ({today} UTC)") and "net +0.60 USDT after fees" in text
        else:
            assert text == f"Daily P&L summary ({today} UTC): 2 trade(s) closed, net +1.00 USDT"  # exactly the old wording


# ------------------------------------------------------------------ the API
@pytest.fixture
def reports_on():
    app.dependency_overrides[get_settings] = lambda: dataclasses.replace(load_settings(), reporting_v2=True)
    yield
    app.dependency_overrides.pop(get_settings, None)


def _seed(email, rows):
    with session_scope() as s:
        cid = s.query(Customer).filter(Customer.email == email).one().id
        aid = s.query(TradingAccount).filter(TradingAccount.customer_id == cid).one().id
        now = datetime.now(timezone.utc)
        for i, (pnl, paper, demo, fee, days_ago) in enumerate(rows):
            s.add(Trade(account_id=aid, side="long", is_paper=paper, is_demo=demo, entry_price=100.0, quantity=1.0, stop_loss_price=95.0, status="closed", pair="BTC/USDT", exit_price=100.0 + pnl, pnl=pnl,
                        entry_fee_quote=fee, exit_fee_quote=fee, opened_at=now - timedelta(days=days_ago, hours=1), closed_at=now - timedelta(days=days_ago), close_reason="x"))


def test_the_report_route_is_off_by_default():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "rep-off@example.com")
        assert client.get("/api/reports/summary").status_code == 409
        assert client.get("/api/account").json()["reports_available"] is False


def test_a_customer_sees_only_their_own_account_and_no_secret(reports_on):
    with TestClient(app, base_url="https://testserver") as mine, TestClient(app, base_url="https://testserver") as theirs:
        _signup_with_account(mine, "rep-mine@example.com")
        _signup_with_account(theirs, "rep-theirs@example.com")
        _seed("rep-mine@example.com", [(2.0, False, False, 0.1, 1), (3.0, True, False, None, 1), (5.0, False, True, None, 1)])
        _seed("rep-theirs@example.com", [(1000.0, False, False, 1.0, 1)])
        r = mine.get("/api/reports/summary")
        assert r.status_code == 200
        body = r.json()
        by_mode = {m["mode"]: m["total"] for m in body["modes"]}
        assert by_mode["live"]["gross_pnl"] == 2.0 and by_mode["live"]["net_pnl"] == 1.8 and by_mode["demo"]["gross_pnl"] == 5.0 and by_mode["paper"]["gross_pnl"] == 3.0
        assert "1000" not in r.text  # the other customer's trade is not in this report
        text = r.text.lower()
        for forbidden in ("api_key", "secret", "password", "email", "token", "rep-mine", "account_id", "encrypted"):
            assert forbidden not in text, forbidden
        assert mine.get("/api/account").json()["reports_available"] is True
        assert {m["mode"]: m["total"]["gross_pnl"] for m in theirs.get("/api/reports/summary").json()["modes"]} == {"live": 1000.0}


def test_the_window_limits_the_report_and_no_account_can_be_named(reports_on):
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "rep-window@example.com")
        _seed("rep-window@example.com", [(1.0, False, False, 0.1, 1), (10.0, False, False, 0.1, 40)])
        assert client.get("/api/reports/summary?window_days=7").json()["modes"][0]["total"]["gross_pnl"] == 1.0
        assert client.get("/api/reports/summary").json()["modes"][0]["total"]["gross_pnl"] == 11.0
        assert client.get("/api/reports/summary?window_days=0").status_code == 422
        other = client.get("/api/reports/summary?account_id=someone-else")
        assert other.status_code == 200 and other.json()["modes"][0]["total"]["gross_pnl"] == 11.0  # an unknown parameter changes nothing: there is no way to ask for another account


def test_the_route_needs_a_login():
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/reports/summary").status_code in (401, 403)


# ------------------------------------------------------------------ end to end with the real fee columns
def test_a_futures_trade_closed_through_the_lifecycle_reports_net_from_the_captured_fees(db, monkeypatch):
    st = make_settings(max_notional_multiplier=100.0, protection_precheck_mode="off")
    client = futures_client()
    install(monkeypatch, client)
    aid = new_account(st, market_type="futures", email="e2e-rep@example.com")
    run_cycle(aid, st)
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    run_cycle(aid, st)
    t = the_trade(aid)
    assert t.status == "closed" and t.entry_fee_quote is not None and t.exit_fee_quote is not None  # PR fee capture filled both at close
    live = rp.build([t])["modes"][0]["total"]
    expected_fees = sum(f["fee"]["cost"] for f in client.fills)
    assert live["fees_complete"] is True and abs(live["fees"] - expected_fees) < 1e-6 and abs(live["net_pnl"] - (t.pnl - expected_fees)) < 1e-6


def test_the_screen_never_shows_a_net_the_server_did_not_send():
    import re
    from pathlib import Path

    front = Path(__file__).resolve().parent.parent / "frontend" / "src"
    panel, perf, report_js = (front / "components" / "ReportPanel.jsx").read_text(), (front / "pages" / "Performance.jsx").read_text(), (front / "lib" / "report.js").read_text()
    assert "<ReportPanel account={account} />" in perf and "account.reports_available" in panel and "report.funding_note" in panel
    assert "net_pnl" in report_js and not re.search(r"gross_pnl\s*-\s*", report_js + panel)  # the screen never computes a net itself
