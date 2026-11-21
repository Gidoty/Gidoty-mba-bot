"""Commercial readiness, API side: SIGNUPS_MODE, version bumps and re-acceptance, the Risk Disclosure acceptance, and customer data requests.

Same isolated-app-per-module pattern as test_legal_consent.py. Settings are read on every request, so the switches are set with monkeypatch.setenv."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_commercial_api.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

import mba_bot.api.routers.auth as auth_router  # noqa: E402
from mba_bot import config, legal  # noqa: E402
from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.routers import privacy  # noqa: E402
from mba_bot.api.security import hash_password  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.models import Customer, DataRequest, LegalAcceptance, TradingAccount  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PW = "correct-horse-battery"
OLD = "2026-10-18"


def _signup(client, email):
    return client.post("/api/auth/signup", json={"email": email, "password": PW, "accept_terms": True, "acknowledge_privacy": True})


def _accepted(email):
    with session_scope() as s:
        c = s.query(Customer).filter(Customer.email == email).one()
        return {(a.document_type, a.version) for a in s.query(LegalAcceptance).filter(LegalAcceptance.customer_id == c.id).all()}


def _customer(email, *, accept: dict | None = None, live: bool = False) -> str:
    """A customer row made directly (as the migration leaves existing customers), with the given acceptances {document: version} and optionally live trading enabled."""
    with session_scope() as s:
        c = Customer(email=email, password_hash=hash_password(PW))
        s.add(c)
        s.flush()
        for doc, version in (accept or {}).items():
            s.add(LegalAcceptance(customer_id=c.id, document_type=doc, version=version, accepted_at=datetime.now(timezone.utc)))
        if live:
            now = datetime.now(timezone.utc)
            s.add(TradingAccount(customer_id=c.id, pair="BTC/USDT", market_type="spot", notification_channel="telegram", trial_ends_at=now - timedelta(days=1), live_trading_enabled_at=now, bot_code="MBA-" + c.id[:6].upper()))
        return c.id


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200
    return r.json()


CURRENT = {"terms": legal.TERMS_VERSION, "privacy": legal.PRIVACY_VERSION, "risk_disclosure": legal.RISK_DISCLOSURE_VERSION}
STALE = {"terms": OLD, "privacy": OLD, "risk_disclosure": OLD}


# ------------------------------------------------------------------------------------------------ 1. SIGNUPS_MODE
@pytest.mark.parametrize("raw, mode", [(None, "open"), ("", "open"), ("open", "open"), (" OPEN ", "open"), ("closed", "closed"), ("CLOSED", "closed"), ("close", "closed"), ("clsoed", "closed"), ("no", "closed"), ("0", "closed")])
def test_signups_mode_parsing_default_open_and_a_mistyped_value_is_closed(raw, mode):
    assert config.parse_signups_mode(raw) == mode


def test_the_default_is_open_so_nothing_changes(monkeypatch):
    monkeypatch.delenv("SIGNUPS_MODE", raising=False)
    assert config.load_settings().signups_mode == "open"
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/auth/signups").json() == {"open": True, "message": None}
        assert _signup(client, "open-signup@example.com").status_code == 201


def test_closed_refuses_a_new_account_with_a_plain_message_and_a_contact_line_and_creates_nothing(monkeypatch):
    monkeypatch.setenv("SIGNUPS_MODE", "closed")
    with TestClient(app, base_url="https://testserver") as client:
        r = _signup(client, "stranger@example.com")
        assert r.status_code == 403
        detail = r.json()["detail"]
        assert "closed" in detail and legal.OPERATOR_EMAIL in detail and "log in" in detail.lower()
        assert "set-cookie" not in r.headers  # no session was started
        status = client.get("/api/auth/signups").json()
        assert status["open"] is False and legal.OPERATOR_EMAIL in status["message"]
    with session_scope() as s:
        assert s.query(Customer).filter(Customer.email == "stranger@example.com").first() is None
        assert s.query(LegalAcceptance).join(Customer, Customer.id == LegalAcceptance.customer_id).filter(Customer.email == "stranger@example.com").count() == 0


def test_closed_refuses_even_a_body_that_would_otherwise_be_valid_and_an_existing_email_is_not_revealed(monkeypatch):
    with TestClient(app, base_url="https://testserver") as client:
        assert _signup(client, "already-here@example.com").status_code == 201
    monkeypatch.setenv("SIGNUPS_MODE", "closed")
    with TestClient(app, base_url="https://testserver") as client:
        r = _signup(client, "already-here@example.com")
        assert r.status_code == 403 and "already exists" not in r.json()["detail"]  # same answer for everyone: no account enumeration through the closed door


def test_closed_still_allows_login_me_and_the_account_routes_for_existing_customers(monkeypatch):
    with TestClient(app, base_url="https://testserver") as client:
        assert _signup(client, "member@example.com").status_code == 201
    monkeypatch.setenv("SIGNUPS_MODE", "closed")
    with TestClient(app, base_url="https://testserver") as client:
        me = _login(client, "member@example.com")
        assert me["email"] == "member@example.com" and me["legal_acceptance_current"] is True
        assert client.get("/api/auth/me").status_code == 200
        assert client.post("/api/account", json={"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram"}).status_code == 201
        assert client.get("/api/account").status_code == 200
        assert client.post("/api/auth/logout").status_code == 204
        assert _login(client, "member@example.com")["email"] == "member@example.com"  # and again after logging out


def test_closed_leaves_a_registered_unpaid_customer_with_the_activate_screen(monkeypatch):
    with TestClient(app, base_url="https://testserver") as client:
        assert _signup(client, "unpaid@example.com").status_code == 201
    monkeypatch.setenv("SIGNUPS_MODE", "closed")
    monkeypatch.setenv("SUBSCRIPTION_BILLING_ENABLED", "true")
    with TestClient(app, base_url="https://testserver") as client:
        me = _login(client, "unpaid@example.com")
        assert me["has_paid"] is False  # the screens show Activate for this
        options = client.get("/api/billing/options")
        assert options.status_code == 200 and options.json()["state"] == "none"  # the Activate offer is still served


def test_signups_mode_is_read_only_where_a_customer_is_created():
    """The mode is consulted in config and in the one place a customer is created. Nowhere else: login, billing and the admin dashboard cannot be closed by it."""
    users = {str(p.relative_to(ROOT / "src")) for p in (ROOT / "src").rglob("*.py") if "signups_mode" in p.read_text()}
    assert users == {"mba_bot/config.py", "mba_bot/api/routers/auth.py"}
    auth = (ROOT / "src/mba_bot/api/routers/auth.py").read_text()
    login_src = auth[auth.index("def login("):auth.index('@router.post("/logout"')]
    assert "signups_mode" not in login_src


# ------------------------------------------------------------------------------------------------ 3. versions and consent
def _js_const(name):
    return re.search(rf'export const {name} = "([0-9-]+)"', (ROOT / "frontend/src/legalContent.js").read_text()).group(1)


def test_the_versions_were_bumped_and_the_frontend_texts_carry_the_same_ones():
    assert legal.TERMS_VERSION == _js_const("TERMS_VERSION") != OLD
    assert legal.PRIVACY_VERSION == _js_const("PRIVACY_VERSION") != OLD
    assert legal.RISK_DISCLOSURE_VERSION == _js_const("RISK_DISCLOSURE_VERSION") != OLD
    js = (ROOT / "frontend/src/legalContent.js").read_text()
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    for version, title in ((legal.TERMS_VERSION, "Terms of Service"), (legal.PRIVACY_VERSION, "Privacy Policy"), (legal.RISK_DISCLOSURE_VERSION, "Trading Risk Disclosure")):
        y, m, d = (int(x) for x in version.split("-"))
        assert f'effectiveDate: "{d} {months[m - 1]} {y}"' in js, title


def test_old_acceptances_do_not_count_after_a_bump_the_customer_is_gated_until_they_accept():
    _customer("oldversion@example.com", accept={"terms": OLD, "privacy": OLD})
    with TestClient(app, base_url="https://testserver") as client:
        me = _login(client, "oldversion@example.com")
        assert me["legal_acceptance_current"] is False and me["risk_acceptance_needed"] is False
        r = client.get("/api/account")
        assert r.status_code == 403 and r.json()["detail"].startswith("ACCEPTANCE_REQUIRED")
        ok = client.post("/api/auth/accept-legal", json={"accept_terms": True, "acknowledge_privacy": True})
        assert ok.status_code == 200 and ok.json()["legal_acceptance_current"] is True
    assert {("terms", OLD), ("privacy", OLD), ("terms", legal.TERMS_VERSION), ("privacy", legal.PRIVACY_VERSION)} <= _accepted("oldversion@example.com")  # the old rows stay as history; new ones are added


def test_the_next_bump_forces_re_acceptance_again(monkeypatch):
    _customer("nextbump@example.com", accept=CURRENT)
    with TestClient(app, base_url="https://testserver") as client:
        assert _login(client, "nextbump@example.com")["legal_acceptance_current"] is True
        monkeypatch.setattr(legal, "TERMS_VERSION", "2099-01-01")
        monkeypatch.setattr(auth_router, "TERMS_VERSION", "2099-01-01")
        assert client.get("/api/auth/me").json()["legal_acceptance_current"] is False
        assert client.get("/api/account").status_code == 403
        assert client.post("/api/auth/accept-legal", json={"accept_terms": True, "acknowledge_privacy": True}).json()["legal_acceptance_current"] is True
    assert ("terms", "2099-01-01") in _accepted("nextbump@example.com")


def test_a_customer_who_never_enabled_live_is_not_asked_for_the_risk_disclosure():
    _customer("nolive@example.com", accept={"terms": legal.TERMS_VERSION, "privacy": legal.PRIVACY_VERSION})
    with TestClient(app, base_url="https://testserver") as client:
        me = _login(client, "nolive@example.com")
        assert me["legal_acceptance_current"] is True and me["risk_acceptance_needed"] is False


def test_a_live_customer_must_re_accept_the_risk_disclosure_and_it_is_recorded_with_version_and_time():
    _customer("livecustomer@example.com", accept={"terms": legal.TERMS_VERSION, "privacy": legal.PRIVACY_VERSION, "risk_disclosure": OLD}, live=True)
    with TestClient(app, base_url="https://testserver") as client:
        me = _login(client, "livecustomer@example.com")
        assert me["legal_acceptance_current"] is False and me["risk_acceptance_needed"] is True
        assert client.get("/api/account").status_code == 403
        refused = client.post("/api/auth/accept-legal", json={"accept_terms": True, "acknowledge_privacy": True})  # the box for the Risk Disclosure is missing
        assert refused.status_code == 422 and "risk" in refused.json()["detail"].lower()
        assert ("risk_disclosure", legal.RISK_DISCLOSURE_VERSION) not in _accepted("livecustomer@example.com")
        before = datetime.now(timezone.utc)
        ok = client.post("/api/auth/accept-legal", json={"accept_terms": True, "acknowledge_privacy": True, "accept_risk_disclosure": True})
        assert ok.status_code == 200 and ok.json()["legal_acceptance_current"] is True and ok.json()["risk_acceptance_needed"] is False
        assert client.get("/api/account").status_code != 403
    with session_scope() as s:
        c = s.query(Customer).filter(Customer.email == "livecustomer@example.com").one()
        row = s.query(LegalAcceptance).filter(LegalAcceptance.customer_id == c.id, LegalAcceptance.document_type == "risk_disclosure", LegalAcceptance.version == legal.RISK_DISCLOSURE_VERSION).one()
        accepted = row.accepted_at if row.accepted_at.tzinfo else row.accepted_at.replace(tzinfo=timezone.utc)
        assert before - timedelta(seconds=5) <= accepted <= datetime.now(timezone.utc) + timedelta(seconds=5)
        assert ("risk_disclosure", OLD) in {(a.document_type, a.version) for a in s.query(LegalAcceptance).filter(LegalAcceptance.customer_id == c.id).all()}  # history kept


def test_a_live_customer_with_no_risk_acceptance_at_all_is_asked_too():
    _customer("liveno-risk@example.com", accept={"terms": legal.TERMS_VERSION, "privacy": legal.PRIVACY_VERSION}, live=True)
    with TestClient(app, base_url="https://testserver") as client:
        assert _login(client, "liveno-risk@example.com")["risk_acceptance_needed"] is True


def _go_live_ready(client, email):
    assert _signup(client, email).status_code == 201
    assert client.post("/api/account", json={"pair": "BTC/USDT", "market_type": "spot", "notification_channel": "telegram", "api_key": "k", "api_secret": "s"}).status_code == 201
    with session_scope() as s:
        s.query(Customer).filter(Customer.email == email).one().has_paid = True
    assert client.put("/api/account/credentials", json={"api_key": "real-key", "api_secret": "real-secret", "environment": "mainnet"}).status_code == 200
    with session_scope() as s:
        s.query(Customer).filter(Customer.email == email).one().account.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
    assert client.patch("/api/account", json={"daily_loss_limit_usd": 5}).status_code == 200


def test_enable_live_records_the_risk_acceptance_with_the_current_version():
    with TestClient(app, base_url="https://testserver") as client:
        _go_live_ready(client, "firstlive@example.com")
        r = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True, "risk_disclosure_version": legal.RISK_DISCLOSURE_VERSION})
        assert r.status_code == 200
    assert ("risk_disclosure", legal.RISK_DISCLOSURE_VERSION) in _accepted("firstlive@example.com")


def test_enable_live_refuses_an_acceptance_of_a_text_the_customer_did_not_see():
    with TestClient(app, base_url="https://testserver") as client:
        _go_live_ready(client, "staleclient@example.com")
        r = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True, "risk_disclosure_version": OLD})
        assert r.status_code == 409 and "updated" in r.json()["detail"]
    assert ("risk_disclosure", legal.RISK_DISCLOSURE_VERSION) not in _accepted("staleclient@example.com") and ("risk_disclosure", OLD) not in _accepted("staleclient@example.com")
    with session_scope() as s:
        assert s.query(Customer).filter(Customer.email == "staleclient@example.com").one().account.live_trading_enabled_at is None


# ------------------------------------------------------------------------------------------------ 4. data requests
def test_a_customer_can_record_an_export_and_a_deletion_request_and_nothing_is_deleted():
    _customer("datareq@example.com", accept=CURRENT)
    with TestClient(app, base_url="https://testserver") as client:
        _login(client, "datareq@example.com")
        e = client.post("/api/privacy/requests", json={"kind": "export"})
        d = client.post("/api/privacy/requests", json={"kind": "deletion"})
        assert e.status_code == d.status_code == 200
        assert e.json()["status"] == "open" and "copy" in e.json()["message"] and legal.OPERATOR_EMAIL in e.json()["message"]
        msg = d.json()["message"]
        assert "Nothing is deleted automatically" in msg
        for what, _why in privacy.RETAINED_RECORDS:
            assert what in msg  # the reply states which records are kept
        again = client.post("/api/privacy/requests", json={"kind": "deletion"})
        assert again.json()["id"] == d.json()["id"]  # a second click returns the open request: no duplicate
        assert len(client.get("/api/privacy/requests").json()) == 2
        assert client.post("/api/privacy/requests", json={"kind": "everything"}).status_code == 422
        assert client.get("/api/auth/me").status_code == 200  # the account is untouched
    with session_scope() as s:
        c = s.query(Customer).filter(Customer.email == "datareq@example.com").one()
        assert c.is_active and s.query(DataRequest).filter(DataRequest.customer_id == c.id).count() == 2


def test_data_requests_need_a_login_but_not_the_current_terms_acceptance():
    with TestClient(app, base_url="https://testserver") as client:
        assert client.post("/api/privacy/requests", json={"kind": "export"}).status_code == 401
    _customer("datareq-stale@example.com", accept={"terms": OLD, "privacy": OLD})
    with TestClient(app, base_url="https://testserver") as client:
        assert _login(client, "datareq-stale@example.com")["legal_acceptance_current"] is False
        assert client.post("/api/privacy/requests", json={"kind": "deletion"}).status_code == 200  # a customer who has not accepted the new version can still ask for their data


def test_a_customer_sees_only_their_own_requests():
    _customer("owner-a@example.com", accept=CURRENT)
    _customer("owner-b@example.com", accept=CURRENT)
    with TestClient(app, base_url="https://testserver") as client:
        _login(client, "owner-a@example.com")
        client.post("/api/privacy/requests", json={"kind": "export"})
    with TestClient(app, base_url="https://testserver") as client:
        _login(client, "owner-b@example.com")
        assert client.get("/api/privacy/requests").json() == []


def test_the_privacy_text_lists_exactly_the_records_the_route_says_are_kept():
    js = (ROOT / "frontend/src/legalContent.js").read_text()
    for what, why in privacy.RETAINED_RECORDS:
        head = what.split(",")[0].split(" (")[0]
        assert head.lower().replace("your ", "") in js.lower().replace("your ", ""), what
    assert "no automatic deletion" in js


def test_the_risk_acceptance_row_carries_the_risk_disclosure_version_not_another_documents(monkeypatch):
    """The three versions are equal today, so a mix-up would be invisible. Give the Risk Disclosure its own version and check the row says so."""
    monkeypatch.setattr(legal, "RISK_DISCLOSURE_VERSION", "2099-02-02")
    monkeypatch.setattr(auth_router, "RISK_DISCLOSURE_VERSION", "2099-02-02")
    _customer("ownversion@example.com", accept={"terms": legal.TERMS_VERSION, "privacy": legal.PRIVACY_VERSION, "risk_disclosure": legal.RISK_DISCLOSURE_VERSION.replace("2099-02-02", OLD)}, live=True)
    with TestClient(app, base_url="https://testserver") as client:
        assert _login(client, "ownversion@example.com")["risk_acceptance_needed"] is True
        assert client.post("/api/auth/accept-legal", json={"accept_terms": True, "acknowledge_privacy": True, "accept_risk_disclosure": True}).status_code == 200
    accepted = _accepted("ownversion@example.com")
    assert ("risk_disclosure", "2099-02-02") in accepted and ("risk_disclosure", legal.TERMS_VERSION) not in accepted
