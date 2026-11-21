"""The live legal texts say only what the code does: numbers are checked against the code's own constants, the cancel wording is shared, no regulatory claim is made, and the operator tool for data requests
exports no secret."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mba_bot import config, data_requests, leverage, legal, risk_limits
from mba_bot.db import init_db, session_scope
from mba_bot.models import ApiCredential, BillingPayment, Customer, DataRequest, LegalAcceptance, Subscription, Trade, TradingAccount

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "frontend/src/legalContent.js").read_text()
TEXT = "\n".join(line for line in JS.splitlines() if not line.strip().startswith("//"))  # what a customer can read: no code comments


def _doc(name: str) -> str:
    """The text of one exported document (TERMS_OF_SERVICE, RISK_DISCLOSURE, PRIVACY_POLICY)."""
    start = TEXT.index(f"export const {name} = {{")
    nxt = [TEXT.index(f"export const {n} = {{") for n in ("TERMS_OF_SERVICE", "RISK_DISCLOSURE", "PRIVACY_POLICY") if TEXT.index(f"export const {n} = {{") > start]
    return TEXT[start:min(nxt) if nxt else len(TEXT)]


# ------------------------------------------------------------------------------------------------ numbers match the code
def test_the_numbers_in_the_texts_are_the_numbers_in_the_code():
    terms, risk = _doc("TERMS_OF_SERVICE"), _doc("RISK_DISCLOSURE")
    for doc in (terms, risk):
        assert f"{risk_limits.RISK_MIN}%" in doc and f"{risk_limits.RISK_CEILING:g}%" in doc  # 0.1% to 40%
        assert f"{risk_limits.TOTAL_STOP_PCT:g}% or more below" in doc  # the fixed total loss stop
        assert f"{risk_limits.RISK_RECOMMENDED_MAX:g}%" in doc and f"{risk_limits.RISK_VERY_HIGH_FROM:g}%" in doc  # confirmations above 2% and from 10%
        assert "1x to 5x" in doc
    assert (leverage.LEVERAGE_MIN, leverage.LEVERAGE_MAX) == (1, 5) and leverage.LEVERAGE_DEFAULT == 3 and "3x if you never set one" in terms
    assert f"currently {config.Settings.subscription_grace_days} days" in terms  # the grace period default
    assert config.load_settings().subscription_grace_days == config.Settings.subscription_grace_days
    assert "The default is 1%" in terms and "default is 1%" in risk


def test_the_cancel_wording_is_the_one_the_billing_screen_shows():
    billing_js = (ROOT / "frontend/src/lib/billing.js").read_text()
    wording = re.search(r'CANCEL_WORDING = "([^"]+)"', billing_js).group(1)
    assert wording in _doc("TERMS_OF_SERVICE")


def test_the_operator_is_named_the_same_everywhere():
    for doc in ("TERMS_OF_SERVICE", "PRIVACY_POLICY"):
        text = _doc(doc)
        assert legal.OPERATOR_NAME in text and legal.OPERATOR_WEBSITE in text and legal.OPERATOR_EMAIL in text
    assert legal.OPERATOR_EMAIL.endswith("@" + legal.OPERATOR_WEBSITE)


# ------------------------------------------------------------------------------------------------ what the texts must not say
@pytest.mark.parametrize("word", ["licensed", "licenced", "registered", "regulated", "approved", "exempt", "authorised", "authorized by", "supervised"])
def test_no_regulatory_status_claim_in_any_document(word):
    assert re.search(rf"\b{word}\b", TEXT, re.IGNORECASE) is None, word  # neither "is licensed" nor "is not licensed": the status sentence is the operator's to add


def test_the_regulatory_status_slot_is_marked_for_the_operator():
    assert JS.count("REGULATORY-STATUS-SLOT") == 1


def test_no_profit_promise_and_no_owner_specific_text():
    low = TEXT.lower()
    for bad in ("guaranteed profit", "risk-free", "risk free", "we guarantee", "sure profit", "27624db9", "pilot", "lawyer", "draft"):
        assert bad not in low, bad
    assert "does not give personal investment advice" in low and "total loss" in low
    assert "never enable withdrawal permission" in low or "do not enable withdrawal permission" in low


def test_the_texts_state_what_happens_to_open_trades_when_payment_stops():
    terms = _doc("TERMS_OF_SERVICE")
    assert "Trades that are already open stay open" in terms and "stops opening new live trades" in terms
    assert "keeps being managed" in terms or "keep being managed" in terms


# ------------------------------------------------------------------------------------------------ the operator tool for data requests
@pytest.fixture
def populated(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/dr.db"
    init_db(url)
    monkeypatch.setenv("DATABASE_URL", url)
    with session_scope() as s:
        c = Customer(email="export-me@example.com", password_hash="PLANTED-PASSWORD-HASH", has_paid=True)
        s.add(c)
        s.flush()
        a = TradingAccount(customer_id=c.id, pair="BTC/USDT", market_type="spot", notification_channel="telegram", notification_target="PLANTED-CHAT-ID", bot_code="MBA-TESTEX", trial_ends_at=datetime.now(timezone.utc))
        s.add(a)
        s.flush()
        s.add(ApiCredential(account_id=a.id, exchange="binance", environment="mainnet", encrypted_api_key="PLANTED-ENC-KEY", encrypted_api_secret="PLANTED-ENC-SECRET"))
        s.add(Trade(account_id=a.id, side="long", is_paper=True, entry_price=1.0, quantity=1.0, stop_loss_price=0.5, status="closed", pair="BTC/USDT", pnl=1.5))
        s.add(LegalAcceptance(customer_id=c.id, document_type="terms", version="2026-11-21", accepted_at=datetime.now(timezone.utc)))
        s.add(Subscription(customer_id=c.id, status="active", provider="paystack", provider_subscription_ref="PLANTED-SUB-REF"))
        s.add(BillingPayment(customer_id=c.id, provider="paystack", reference="PLANTED-PAY-REF", amount_minor=1500000, currency="NGN", status="paid", provider_payment_id="PLANTED-PROVIDER-ID"))
        s.add(DataRequest(customer_id=c.id, kind="deletion"))
        return c.id


def test_list_shows_open_requests_without_any_e_mail(populated, capsys):
    assert data_requests.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "deletion" in out and populated[:8] in out and "example.com" not in out and populated not in out


def test_the_export_holds_the_customers_data_and_none_of_the_secrets(populated, capsys):
    assert data_requests.main(["export", "--customer", populated[:8]]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["customer"]["email"] == "export-me@example.com" and data["account"]["pair"] == "BTC/USDT"
    assert data["trades"][0]["pnl"] == 1.5 and data["acceptances"][0]["version"] == "2026-11-21" and data["payments"][0]["amount_minor"] == 1500000
    assert data["telegram_linked"] is True and data["data_requests"][0]["kind"] == "deletion"
    assert "PLANTED" not in out  # no password hash, key, secret, chat id, provider reference or token


def test_the_export_and_list_change_nothing_and_never_overwrite(populated, tmp_path, capsys):
    def counts():
        with session_scope() as s:
            return [s.query(m).count() for m in (Customer, TradingAccount, Trade, DataRequest, LegalAcceptance, ApiCredential)]

    before = counts()
    out = tmp_path / "export.json"
    assert data_requests.main(["export", "--customer", populated[:8], "--out", str(out)]) == 0
    assert json.loads(out.read_text())["customer"]["email"] == "export-me@example.com"
    assert data_requests.main(["export", "--customer", populated[:8], "--out", str(out)]) == 2  # the file exists: not overwritten
    assert data_requests.main(["list"]) == 0
    assert counts() == before


def test_a_short_or_unknown_id_and_a_missing_database_are_refused_without_a_message_that_leaks(populated, tmp_path, monkeypatch, capsys):
    assert data_requests.main(["export", "--customer", "abc"]) == 2
    assert data_requests.main(["export", "--customer", "zzzzzzzz"]) == 2
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/missing.db")
    assert data_requests.main(["list"]) == 2
    assert not (tmp_path / "missing.db").exists()
    assert "export-me" not in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------ the checklist document
CHECKLIST = (ROOT / "docs/commercial_readiness.md").read_text()
SWITCHES = ["SIGNUPS_MODE", "SUBSCRIPTION_BILLING_ENABLED", "NOWPAYMENTS_ENABLED", "ADMIN_DASHBOARD_ENABLED", "ACCOUNT_DAILY_LOSS_LIMIT", "ACCOUNT_TOTAL_LOSS_STOP", "PROTECTION_PRECHECK_MODE",
            "EMA_CLOSED_CANDLE_ONLY", "LIVE_RSI_MR", "LIVE_DONCHIAN", "LIVE_MULTI_POSITION", "REPORTING_V2", "SETUP_ADVISOR", "RISK_PCT_MAX"]


@pytest.mark.parametrize("name", SWITCHES)
def test_every_listed_switch_is_in_the_checklist_and_is_read_by_the_code(name):
    assert f"`{name}`" in CHECKLIST
    assert f'"{name}"' in (ROOT / "src/mba_bot/config.py").read_text(), name  # a renamed or removed variable fails here, not on the server


def test_the_checklist_names_no_customer_and_no_private_material():
    low = CHECKLIST.lower()
    for bad in ("27624db9", "handoff", "audit", "lawyer", "owner"):
        assert bad not in low, bad


def test_the_data_requests_migration_is_one_idempotent_table_on_top_of_the_alert_marks_one(tmp_path):
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = next((ROOT / "alembic" / "versions").glob("c3f8a2d5b7e9_*.py"))
    spec = importlib.util.spec_from_file_location("data_requests_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "b2e7d4a9c6f1"
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")

    def run(fn):
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    run(mig.upgrade)
    run(mig.upgrade)  # idempotent
    assert inspect(eng).get_table_names() == ["data_requests"]
    assert {c["name"] for c in inspect(eng).get_columns("data_requests")} == {"id", "customer_id", "kind", "status", "requested_at", "handled_at"}
    run(mig.downgrade)
    assert inspect(eng).get_table_names() == []
