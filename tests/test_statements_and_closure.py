"""Trade statements (CSV) and completing an account deletion (docs/safety/statements_and_closure.md). Fake exchange nothing: an isolated database only.

PROVES: the CSV has gross, fees, funding and net as separate columns, fills each only when it is really known (fees need both fills and never for practice, net needs the fees, funding is always empty), says why in a
note, never mixes customers, filters by mode and date, cannot run as a spreadsheet formula, and is capped; the deletion flow refuses unless every rule holds (switches, step-up, an approved deletion request, the exact
confirmation, no open trade, not an admin), erases every table that holds the customer's data (derived from the schema so a new table is erased by default), keeps only what the Privacy text lists, anonymises the
customer row so the old password and email no longer work, leaves other customers untouched and places or changes no trade. DOES NOT PROVE anything on a real server or at a payment provider."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from admin_support import ADMIN_EMAIL, NOW, client_for, make_env, mk_account, mk_customer, mk_trade
from mba_bot import account_closure as AC, admin_auth, reporting, statement
from mba_bot.api.routers import admin as admin_router, privacy
from mba_bot.api.security import hash_password, verify_password
from mba_bot.db import session_scope
from mba_bot.models import (AccountDayEquity, AdminChange, ApiCredential, Base, BillingPayment, Customer, CustomerNote, DataRequest, DecisionLog, LegalAcceptance, PairSkip, Subscription,
                            SubscriptionToken, Trade, TradeTelemetry, TradingAccount)

import pyotp


# ---------------------------------------------------------------- the statement
class T:
    def __init__(self, **kw):
        self.__dict__.update(dict(id="abcdef123456", status="closed", pair="BTC/USDT", side="long", is_paper=False, is_demo=False, strategy_id=None, opened_at=datetime(2026, 1, 1, 10), closed_at=datetime(2026, 1, 2, 10),
                                  entry_price=100.0, exit_price=110.0, quantity=1.5, close_reason="take_profit", pnl=15.0, entry_fee_quote=0.05, exit_fee_quote=0.06))
        self.__dict__.update(kw)


def parse(text_):
    return list(csv.DictReader(io.StringIO(text_)))


def test_the_columns_are_gross_fees_funding_and_net_and_each_is_filled_only_when_known():
    rows = parse(statement.build_csv([T()])[0])
    r = rows[0]
    assert list(r) == list(statement.COLUMNS) and {"gross_pnl", "fees", "funding", "net_pnl"} <= set(r)
    assert r["gross_pnl"] == "15" and r["entry_fee"] == "0.05" and r["exit_fee"] == "0.06" and r["fees"] == "0.11" and r["net_pnl"] == "14.89" and r["funding"] == ""
    assert "funding is not recorded" in r["note"] and r["currency"] == "USDT" and r["trade_ref"] == "abcdef12" and r["mode"] == "live"


def test_missing_fees_leave_fees_and_net_empty_and_practice_trades_never_have_them():
    one = parse(statement.build_csv([T(exit_fee_quote=None)])[0])[0]
    assert one["gross_pnl"] == "15" and one["entry_fee"] == "" and one["fees"] == "" and one["net_pnl"] == "" and "fees not captured" in one["note"]
    paper = parse(statement.build_csv([T(is_paper=True)])[0])[0]
    assert paper["mode"] == "paper" and paper["fees"] == "" and paper["net_pnl"] == "" and "simulated" in paper["note"]
    none = parse(statement.build_csv([T(pnl=None)])[0])[0]
    assert none["gross_pnl"] == "" and none["net_pnl"] == ""
    demo = parse(statement.build_csv([T(is_demo=True)])[0])[0]
    assert demo["mode"] == "demo" and demo["net_pnl"] == "14.89"


def test_a_loss_and_a_negative_number_are_plain_numbers_not_text():
    r = parse(statement.build_csv([T(pnl=-3.5, entry_fee_quote=0.0, exit_fee_quote=0.0)])[0])[0]
    assert r["gross_pnl"] == "-3.5" and r["net_pnl"] == "-3.5" and r["fees"] == "0"


@pytest.mark.parametrize("evil", ["=HYPERLINK(\"http://evil\")", "+1+1", "-2+3", "@SUM(A1)", "\t=1", "\r=1"])
def test_text_cells_cannot_run_as_formulas(evil):
    r = parse(statement.build_csv([T(pair=evil, close_reason=evil, strategy_id=evil)])[0])[0]
    for k in ("pair", "close_reason", "strategy"):
        assert r[k].startswith("'") and r[k][1:].startswith(evil[:1])


def test_open_trades_are_left_out_and_modes_and_dates_filter():
    trades = [T(id="a1" * 6), T(id="b2" * 6, status="open", closed_at=None), T(id="c3" * 6, is_paper=True, closed_at=datetime(2026, 2, 1)), T(id="d4" * 6, closed_at=datetime(2026, 3, 1))]
    ids = lambda **kw: [r["trade_ref"] for r in parse(statement.build_csv(trades, **kw)[0])]  # noqa: E731
    assert ids() == ["a1a1a1a1", "c3c3c3c3", "d4d4d4d4"]
    assert ids(mode="paper") == ["c3c3c3c3"] and ids(mode="live") == ["a1a1a1a1", "d4d4d4d4"]
    assert ids(start=datetime(2026, 2, 1), end=datetime(2026, 3, 1)) == ["c3c3c3c3"]  # start inclusive, end exclusive
    assert ids(start=datetime(2026, 12, 1)) == []


def test_the_file_is_capped_and_says_so(monkeypatch):
    monkeypatch.setattr(statement, "MAX_ROWS", 3)
    text_, cut = statement.build_csv([T(id=f"{i:012d}") for i in range(5)])
    assert cut is True and len(parse(text_)) == 3
    assert statement.build_csv([T()])[1] is False


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_ACTIONS_ENABLED", "1")
    monkeypatch.setenv("ACCOUNT_DELETION_ENABLED", "1")
    from mba_bot import operator_settings

    operator_settings.reset_cache()
    app, settings = make_env(monkeypatch, tmp_path)

    class E:
        pass

    e = E()
    e.app, e.settings, e.mp = app, settings, monkeypatch
    with session_scope() as s:
        admin = mk_customer(s, ADMIN_EMAIL)
        a = mk_customer(s, "alice@example.com", paid=True)
        b = mk_customer(s, "bob@example.com", paid=True)
        a.password_hash = hash_password("alice-password-1")
        acct_a, acct_b = mk_account(s, a, code="MBA-ALICE1"), mk_account(s, b, code="MBA-BOB001")
        e.admin_id, e.a, e.b, e.acct_a, e.acct_b = admin.id, a.id, b.id, acct_a.id, acct_b.id
        secret = admin_auth.enrol(s, admin.id, encryption_key=settings.encryption_key)[0]
    e.secret = secret
    e.clock = [datetime.now(timezone.utc)]

    def tick():
        e.clock[0] = e.clock[0] + timedelta(seconds=30)
        return e.clock[0]

    monkeypatch.setattr(admin_router, "_now", tick)
    e.code = lambda: pyotp.TOTP(secret).at(e.clock[0] + timedelta(seconds=30))
    e.admin = client_for(app, admin.id, settings, admin=True)
    return e


def seed_alice(env):
    with session_scope() as s:
        acct = s.get(TradingAccount, env.acct_a)
        t1 = mk_trade(s, acct, status="closed", paper=False, pnl=5.0, fees=(0.1, 0.1))
        t2 = mk_trade(s, acct, status="closed", paper=True, pnl=-2.0)
        s.add(TradeTelemetry(trade_id=t1.id, strategy_id="x", strategy_version="1", mode="live", market_type="spot", pair="BTC/USDT", signal_type="long"))
        s.add(DecisionLog(account_id=env.acct_a, trade_id=t1.id, mode="live", market_type="spot", strategy_id="x", stage="entry", outcome="taken", reason_code="ok"))
        s.add(PairSkip(account_id=env.acct_a, pair="BTC/USDT", day_key="2026-01-01", reason="x", count=1))
        s.add(AccountDayEquity(account_id=env.acct_a, day_key="2026-01-01", start_equity=100.0))
        s.add(CustomerNote(customer_id=env.a, admin_id=env.admin_id, note="called alice"))
        sub = Subscription(customer_id=env.a, status="expired", provider="paystack", current_period_end=NOW)
        s.add(sub)
        s.flush()
        s.add(SubscriptionToken(subscription_id=sub.id, token_enc="gAAAA-provider-token"))
        s.add(BillingPayment(customer_id=env.a, provider="paystack", reference="ref-alice", amount_minor=100, currency="NGN", status="paid", issued_at=NOW))
        s.add(LegalAcceptance(customer_id=env.a, document_type="terms", version="v1"))
        req = DataRequest(customer_id=env.a, kind="deletion", status="approved")
        s.add(req)
        s.flush()
        return req.id


def csv_for(env, customer_id, **params):
    c = client_for(env.app, customer_id, env.settings)
    return c.get("/api/statements/trades.csv", params=params)


def test_a_customer_downloads_only_their_own_statement(env):
    seed_alice(env)
    with session_scope() as s:
        mk_trade(s, s.get(TradingAccount, env.acct_b), status="closed", paper=False, pnl=99.0, fees=(1.0, 1.0))
    r = csv_for(env, env.a)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"] and "MBA-ALICE1" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    rows = parse(r.text)
    assert len(rows) == 2 and {x["gross_pnl"] for x in rows} == {"5", "-2"}
    assert "99" not in [x["gross_pnl"] for x in rows]
    assert csv_for(env, env.a, mode="paper").text.count("\n") == 2
    assert csv_for(env, env.a, mode="nope").status_code == 422
    assert csv_for(env, env.a, **{"from": "2026-13-01"}).status_code == 422
    assert csv_for(env, env.a, **{"from": "2026-05-01", "to": "2026-04-01"}).status_code == 422
    from fastapi.testclient import TestClient

    assert TestClient(env.app, base_url="https://testserver").get("/api/statements/trades.csv").status_code == 401


# ---------------------------------------------------------------- the deletion flow
def post(env, rid, **body):
    body.setdefault("reason", "customer asked, ticket 42")
    body.setdefault("code", env.code())
    return env.admin.post(f"/api/admin/requests/{rid}/complete-deletion", json=body)


def counts(env):
    with session_scope() as s:
        out = {}
        for t in Base.metadata.sorted_tables:
            cols = {c.name for c in t.columns}
            if "customer_id" in cols:
                out[t.name] = s.execute(select(func.count()).select_from(t).where(t.c.customer_id == env.a)).scalar_one()
            elif "account_id" in cols:
                out[t.name] = s.execute(select(func.count()).select_from(t).where(t.c.account_id == env.acct_a)).scalar_one()
        return out


def confirm_for(env):
    return AC.confirmation_text(env.a)


def test_the_flow_is_refused_while_either_switch_is_off(env):
    rid = seed_alice(env)
    env.mp.setenv("ACCOUNT_DELETION_ENABLED", "0")
    r = post(env, rid, confirm=confirm_for(env))
    assert r.status_code == 403 and "ACCOUNT_DELETION_ENABLED" in r.json()["detail"]
    env.mp.setenv("ACCOUNT_DELETION_ENABLED", "1")
    env.mp.setenv("ADMIN_ACTIONS_ENABLED", "0")
    assert post(env, rid, confirm=confirm_for(env)).status_code == 403
    assert counts(env)["trades"] == 2


def test_it_needs_the_code_the_confirmation_and_an_approved_deletion_request(env):
    rid = seed_alice(env)
    assert post(env, rid, confirm=confirm_for(env), code="000000").status_code == 401
    assert post(env, rid, confirm="DELETE nothing!").status_code == 422
    assert post(env, rid, confirm=confirm_for(env), reason="no").status_code == 422
    assert post(env, "no-such", confirm="x").status_code == 404
    with session_scope() as s:
        s.get(DataRequest, rid).status = "open"
    r = post(env, rid, confirm=confirm_for(env))
    assert r.status_code == 409 and "approve it first" in r.json()["detail"]
    with session_scope() as s:
        s.get(DataRequest, rid).status, s.get(DataRequest, rid).kind = "approved", "export"
    assert post(env, rid, confirm=confirm_for(env)).status_code == 409
    assert counts(env)["trades"] == 2  # nothing was erased by any refusal


def test_an_open_trade_blocks_it_and_nothing_is_erased(env):
    rid = seed_alice(env)
    with session_scope() as s:
        mk_trade(s, s.get(TradingAccount, env.acct_a), status="open", paper=False, pnl=None)
    r = post(env, rid, confirm=confirm_for(env))
    assert r.status_code == 409 and "still open" in r.json()["detail"]
    c = counts(env)
    assert c["trades"] == 3 and c["customer_notes"] == 1


def test_an_admin_account_cannot_be_deleted_this_way(env):
    with session_scope() as s:
        req = DataRequest(customer_id=env.admin_id, kind="deletion", status="approved")
        s.add(req)
        s.flush()
        rid = req.id
    r = post(env, rid, confirm=AC.confirmation_text(env.admin_id))
    assert r.status_code == 409 and "admin" in r.json()["detail"]


def test_a_completed_deletion_erases_the_personal_data_and_keeps_only_what_the_privacy_text_lists(env):
    rid = seed_alice(env)
    with session_scope() as s:
        mk_trade(s, s.get(TradingAccount, env.acct_b), status="closed", paper=False, pnl=7.0, fees=(0.1, 0.1))
    before = counts(env)
    r = post(env, rid, confirm=confirm_for(env))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["erased"]["trades"] == 2 and body["erased"]["trading_accounts"] == 1 and body["erased"]["customer_notes"] == 1 and body["erased"]["subscription_tokens"] == 1
    after = counts(env)
    for table in AC.KEPT:
        if table in after:
            assert after[table] == before[table], table  # the kept records are all still there
    for table, how in AC.classify().items():
        if how == "erased" and table in after:
            assert after[table] == 0, table
    with session_scope() as s:
        c = s.get(Customer, env.a)
        assert c.email == f"deleted-{env.a[:8]}@deleted.invalid" and c.is_active is False and not verify_password("alice-password-1", c.password_hash)
        assert s.get(DataRequest, rid).status == "done" and s.get(DataRequest, rid).handled_at is not None
        assert s.scalar(select(func.count()).select_from(Trade).where(Trade.account_id == env.acct_b)) == 1  # bob is untouched
        assert s.get(Customer, env.b).email == "bob@example.com" and s.get(TradingAccount, env.acct_b) is not None
        log = s.scalars(select(AdminChange).where(AdminChange.action == "complete_deletion")).one()
        assert log.target == env.a and log.admin_id == env.admin_id and "rows erased" in log.new and "alice" not in (log.old + log.new + log.reason)
    assert body["warnings"] == []  # the subscription row was already expired
    assert post(env, rid, confirm=confirm_for(env)).status_code == 409  # a second run is refused: it is done
    assert client_for(env.app, env.a, env.settings).get("/api/auth/me").status_code == 401  # the old session no longer works


def test_an_active_subscription_gets_a_warning_to_cancel_at_the_provider(env):
    rid = seed_alice(env)
    with session_scope() as s:
        s.scalars(select(Subscription).where(Subscription.customer_id == env.a)).one().status = "active"
    r = post(env, rid, confirm=confirm_for(env))
    assert r.status_code == 200 and any("cancel it at the payment provider" in w for w in r.json()["warnings"])


def test_every_table_is_classified_and_the_kept_ones_match_the_privacy_text():
    cls = AC.classify()
    assert set(cls) == {t.name for t in Base.metadata.sorted_tables}
    assert set(AC.KEPT) <= set(cls) and all(cls[k] == "kept" for k in AC.KEPT)
    for t in Base.metadata.sorted_tables:
        cols = {c.name for c in t.columns}
        if cols & {"customer_id", "account_id"} and t.name not in AC.KEPT and t.name not in AC.UNRELATED and t.name != "customers":
            assert cls[t.name] == "erased", f"{t.name} holds a customer or account id and would be kept"
    kept_why = {why for why in AC.KEPT.values()}
    assert kept_why == {what for what, _ in privacy.RETAINED_RECORDS}
    assert cls["customers"] == "shell" and cls["subscription_tokens"] == "erased" and cls["api_credentials"] == "erased" and cls["demo_credentials"] == "erased"


def test_a_table_added_later_is_erased_by_default(monkeypatch):
    from sqlalchemy import Column, String, Table

    t = Table("future_personal_data", Base.metadata, Column("id", String(36), primary_key=True), Column("customer_id", String(36)))
    try:
        assert AC.classify()["future_personal_data"] == "erased"
    finally:
        Base.metadata.remove(t)


def test_the_closure_code_calls_no_exchange_provider_or_trade_function():
    import inspect

    src = inspect.getsource(AC) + inspect.getsource(statement)
    for forbidden in ("ccxt", "lifecycle", "execute_exit", "create_order", "cancel_order", "httpx", "requests.", "import exchange", "venue"):
        assert forbidden not in src, forbidden
