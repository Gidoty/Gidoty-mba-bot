"""GET /api/trades and /api/trades/summary: the `is_paper` scoping that
keeps Testnet and Live statistics from ever mixing, plus the windowed
P&L fields (today/7d/30d) the Live/Testnet dashboards need. Same
isolated-app-per-module pattern as the other API test modules.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_api_trades_summary.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot.api.app import app  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, Trade  # noqa: E402


def _signup_with_account(client: TestClient, email: str) -> dict:
    client.post(
        "/api/auth/signup",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "accept_terms": True,
            "acknowledge_privacy": True,
        },
    )
    created = client.post(
        "/api/account",
        json={
            "pair": "BTC/USDT",
            "market_type": "spot",
            "notification_channel": "telegram",
            "api_key": "k",
            "api_secret": "s",
        },
    )
    assert created.status_code == 201
    return created.json()


def _seed_trade(email: str, *, is_paper: bool, pnl: float, closed_days_ago: float | None, status: str = "closed") -> None:
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        now = datetime.now(timezone.utc)
        closed_at = None if closed_days_ago is None else now - timedelta(days=closed_days_ago)
        session.add(
            Trade(
                account_id=customer.account.id,
                side="long",
                is_paper=is_paper,
                entry_price=100.0,
                quantity=1.0,
                stop_loss_price=95.0,
                status=status,
                exit_price=100.0 + pnl if status == "closed" else None,
                pnl=pnl if status == "closed" else None,
                opened_at=now - timedelta(days=(closed_days_ago or 0) + 1),
                closed_at=closed_at,
            )
        )


def test_summary_without_is_paper_mixes_everything_for_backward_compatibility():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "mixed@example.com")
        _seed_trade("mixed@example.com", is_paper=True, pnl=10.0, closed_days_ago=0)
        _seed_trade("mixed@example.com", is_paper=False, pnl=20.0, closed_days_ago=0)

        response = client.get("/api/trades/summary")
        assert response.status_code == 200
        assert response.json()["net_pnl"] == 30.0
        assert response.json()["total_trades"] == 2


def test_summary_scoped_to_paper_only_excludes_live_trades():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "paperonly@example.com")
        _seed_trade("paperonly@example.com", is_paper=True, pnl=15.0, closed_days_ago=0)
        _seed_trade("paperonly@example.com", is_paper=False, pnl=-50.0, closed_days_ago=0)

        response = client.get("/api/trades/summary", params={"is_paper": "true"})
        assert response.status_code == 200
        body = response.json()
        assert body["net_pnl"] == 15.0
        assert body["total_trades"] == 1


def test_summary_scoped_to_live_only_excludes_paper_trades():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "liveonly@example.com")
        _seed_trade("liveonly@example.com", is_paper=True, pnl=999.0, closed_days_ago=0)
        _seed_trade("liveonly@example.com", is_paper=False, pnl=-5.0, closed_days_ago=0)

        response = client.get("/api/trades/summary", params={"is_paper": "false"})
        assert response.status_code == 200
        body = response.json()
        assert body["net_pnl"] == -5.0
        assert body["total_trades"] == 1


def test_list_trades_is_paper_filter():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "listfilter@example.com")
        _seed_trade("listfilter@example.com", is_paper=True, pnl=1.0, closed_days_ago=0)
        _seed_trade("listfilter@example.com", is_paper=False, pnl=2.0, closed_days_ago=0)

        all_trades = client.get("/api/trades").json()
        assert len(all_trades) == 2

        paper_only = client.get("/api/trades", params={"is_paper": "true"}).json()
        assert len(paper_only) == 1
        assert paper_only[0]["is_paper"] is True

        live_only = client.get("/api/trades", params={"is_paper": "false"}).json()
        assert len(live_only) == 1
        assert live_only[0]["is_paper"] is False


def test_windowed_pnl_buckets_trades_by_close_date():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "windowed@example.com")
        _seed_trade("windowed@example.com", is_paper=False, pnl=1.0, closed_days_ago=0)  # today
        _seed_trade("windowed@example.com", is_paper=False, pnl=2.0, closed_days_ago=3)  # within 7d
        _seed_trade("windowed@example.com", is_paper=False, pnl=4.0, closed_days_ago=10)  # within 30d, not 7d
        _seed_trade("windowed@example.com", is_paper=False, pnl=8.0, closed_days_ago=40)  # within 90d, not 30d
        _seed_trade("windowed@example.com", is_paper=False, pnl=16.0, closed_days_ago=120)  # outside every window

        body = client.get("/api/trades/summary", params={"is_paper": "false"}).json()
        assert body["pnl_today"] == 1.0
        assert body["pnl_7d"] == 3.0  # today + 3 days ago
        assert body["pnl_30d"] == 7.0  # today + 3d + 10d
        assert body["pnl_90d"] == 15.0  # today + 3d + 10d + 40d
        assert body["net_pnl"] == 31.0  # all-time, every closed trade including the 120-day-old one


def test_open_trades_never_counted_in_pnl_windows():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "openpnl@example.com")
        _seed_trade("openpnl@example.com", is_paper=False, pnl=0.0, closed_days_ago=None, status="open")

        body = client.get("/api/trades/summary", params={"is_paper": "false"}).json()
        assert body["open_trades"] == 1
        assert body["closed_trades"] == 0
        assert body["pnl_today"] == 0.0
        assert body["net_pnl"] == 0.0
