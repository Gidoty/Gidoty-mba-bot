"""Admin dashboard load check on a synthetic database: 10,000 customers (each with one bot), 100,000 trades (closed and some open, live, practice and demo, several strategies), plus subscriptions and payments.

PROVES: every panel is served with one aggregate query (two for results and the customer list) whose database time stays far under the design target, at the size the design names. It prints the timings
(run with -s). The bound asserted is deliberately loose (CI machines vary); the numbers in the pull request come from a run on a normal machine.
DOES NOT PROVE: timing on the production server, or with WAL off and a scheduler writing at the same time (UNVERIFIED)."""
from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, insert

from admin_support import ADMIN_EMAIL, client_for, make_env, mk_customer
from mba_bot import admin_service, db as dbmod
from mba_bot.db import session_scope
from mba_bot.models import BillingPayment, Customer, PairSkip, Subscription, Trade, TradingAccount, ApiCredential

CUSTOMERS, TRADES = 10_000, 100_000
BOUND_SECONDS = 0.5


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    app, settings = make_env(mp, tmp_path_factory.mktemp("load"))
    rng = random.Random(1)
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        admin_id = mk_customer(s, ADMIN_EMAIL).id
        cust, acc, cred = [], [], []
        for i in range(CUSTOMERS):
            cid, aid = str(uuid.uuid4()), str(uuid.uuid4())
            cust.append(dict(id=cid, email=f"load{i}@example.org", password_hash="x", created_at=now - timedelta(minutes=i), is_active=True, has_paid=i % 3 == 0))
            acc.append(dict(id=aid, customer_id=cid, bot_code=f"MBA-{i:06d}", pair="BTC/USDT", market_type="futures" if i % 2 else "spot", pair_selection_mode="manual", notification_channel="telegram",
                            trial_ends_at=now - timedelta(days=1), paper_balance=10000.0, live_trading_enabled_at=now if i % 7 == 0 else None, bot_started_at=now if i % 14 == 0 else None,
                            is_active=i % 11 != 0, last_scanned_at=now - timedelta(seconds=rng.randint(0, 3600)), last_error="x" if i % 13 == 0 else None, risk_pct=1.0, live_slots=1,
                            daily_loss_limit_is_default=False, created_at=now, updated_at=now))
            cred.append(dict(id=str(uuid.uuid4()), account_id=aid, exchange="binance", environment="mainnet" if i % 7 == 0 else "testnet", encrypted_api_key="k", encrypted_api_secret="s", created_at=now))
        s.execute(insert(Customer), cust)
        s.execute(insert(TradingAccount), acc)
        s.execute(insert(ApiCredential), cred)
        trades = []
        strategies = [None, "rsi_mr_v1", "donchian_breakout_v1"]
        for j in range(TRADES):
            a = acc[rng.randrange(CUSTOMERS)]
            kind = rng.choice(["live", "paper", "paper", "demo"])
            openn = j % 500 == 0
            trades.append(dict(id=str(uuid.uuid4()), account_id=a["id"], side="long", is_paper=kind == "paper", is_demo=kind == "demo", entry_price=100.0, quantity=1.0, stop_loss_price=95.0,
                               pnl=None if openn else rng.uniform(-3, 4), status="open" if openn else "closed", pair=f"O{j}/USDT" if openn else f"P{j % 40}/USDT", strategy_id=rng.choice(strategies), opened_at=now - timedelta(days=rng.randint(0, 120)),
                               closed_at=None if openn else now - timedelta(days=rng.randint(0, 120)), entry_fee_quote=0.1 if j % 2 else None, exit_fee_quote=0.1 if j % 2 else None,
                               protection_status="protected" if j % 3 else None, live_slot=None))
        for i in range(0, len(trades), 5000):
            s.execute(insert(Trade), trades[i : i + 5000])
        s.execute(insert(Subscription), [dict(id=str(uuid.uuid4()), customer_id=c["id"], status="active", current_period_end=now + timedelta(days=rng.randint(-10, 30)), provider="paystack", created_at=now, updated_at=now) for c in cust[:3000]])
        s.execute(insert(BillingPayment), [dict(id=str(uuid.uuid4()), customer_id=c["id"], provider="paystack", reference=f"R{i}", amount_minor=100, currency="NGN", status="paid", issued_at=now) for i, c in enumerate(cust[:3000])])
        s.execute(insert(PairSkip), [dict(id=str(uuid.uuid4()), account_id=a["id"], pair="BTC/USDT", reason="pair_below_minimum_capital", day_key=now.date().isoformat(), count=2, first_seen=now, last_seen=now) for a in acc[:2000]])
    yield app, settings, admin_id
    mp.undo()


def test_each_panel_is_fast_at_10000_customers_and_100000_trades(big, capsys):
    app, settings, admin_id = big
    c = client_for(app, admin_id, settings, admin=True)
    timings = []
    for path in ["/api/admin/customers/summary", "/api/admin/bots", "/api/admin/health", "/api/admin/results?window_days=30", "/api/admin/results?window_days=0", "/api/admin/customers?page=1", "/api/admin/customers?page=100",
                 "/api/admin/skipped-pairs", "/api/admin/billing"]:
        admin_service.reset_cache()
        n, db_time = [0], [0.0]
        started = {}

        def before(conn, cursor, statement, parameters, context, executemany):
            n[0] += 1
            started["t"] = time.perf_counter()

        def after(conn, cursor, statement, parameters, context, executemany):
            db_time[0] += time.perf_counter() - started["t"]

        event.listen(dbmod._engine, "before_cursor_execute", before)
        event.listen(dbmod._engine, "after_cursor_execute", after)
        try:
            t0 = time.perf_counter()
            r = c.get(path)
            wall = time.perf_counter() - t0
        finally:
            event.remove(dbmod._engine, "before_cursor_execute", before)
            event.remove(dbmod._engine, "after_cursor_execute", after)
        assert r.status_code == 200, path
        assert n[0] <= 5, (path, n[0])
        timings.append((path, n[0], db_time[0] * 1000, wall * 1000))
        assert db_time[0] < BOUND_SECONDS, (path, db_time[0])
    with capsys.disabled():
        print("\nadmin dashboard timing at 10,000 customers, 100,000 trades (SQLite, no WAL, test machine)")
        print(f"{'path':44} {'statements':>10} {'db ms':>8} {'request ms':>11}")
        for p, k, d, w in timings:
            print(f"{p:44} {k:>10} {d:>8.1f} {w:>11.1f}")
