"""Legal-consent gating: signup acceptance, the existing-customer
catch-up gate on /api/account/*, the risk-disclosure requirement on
enable-live-trading, and proof that none of this weakens the four
pre-existing live-trading safety gates or leaks secrets into the
acceptance record. Same isolated-app-per-module pattern as
test_api_live_trading_safety.py.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_legal_consent.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TELEGRAM_BOT_USERNAME"] = "mba_test_bot"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from mba_bot.api.app import app  # noqa: E402
from mba_bot.api.security import hash_password  # noqa: E402
from mba_bot.db import session_scope  # noqa: E402
from mba_bot.legal import PRIVACY_VERSION, RISK_DISCLOSURE_VERSION, TERMS_VERSION  # noqa: E402
from mba_bot.models import Customer, LegalAcceptance  # noqa: E402


def _mark_paid(email: str) -> None:
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        customer.has_paid = True


def _make_eligible(email: str) -> None:
    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == email).one()
        customer.account.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)


def _create_preexisting_customer_without_consent(email: str, password: str) -> str:
    """Simulates a customer who signed up before these legal documents
    existed: a Customer row with zero LegalAcceptance rows, created
    directly (never through POST /api/auth/signup, which now requires
    consent) - exactly the shape the real migration leaves existing
    production customers in."""
    with session_scope() as session:
        customer = Customer(email=email, password_hash=hash_password(password))
        session.add(customer)
        session.flush()
        return customer.id


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
            "api_key": "some-key",
            "api_secret": "some-secret",
        },
    )
    assert created.status_code == 201
    return created.json()


def _retag_mainnet(client: TestClient) -> dict:
    response = client.put(
        "/api/account/credentials",
        json={"api_key": "real-key", "api_secret": "real-secret", "environment": "mainnet"},
    )
    assert response.status_code == 200
    return response.json()


# --- signup: cannot bypass Terms/Privacy acceptance via a direct API request ---


def test_signup_without_consent_flags_is_rejected_and_creates_no_customer():
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/auth/signup", json={"email": "noconsent@example.com", "password": "correct-horse-battery"}
        )
        assert response.status_code == 422
        with session_scope() as session:
            assert session.query(Customer).filter(Customer.email == "noconsent@example.com").first() is None


def test_signup_with_consent_false_is_rejected():
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/auth/signup",
            json={
                "email": "falseconsent@example.com",
                "password": "correct-horse-battery",
                "accept_terms": False,
                "acknowledge_privacy": False,
            },
        )
        assert response.status_code == 422


def test_signup_with_consent_records_version_and_timestamp():
    with TestClient(app, base_url="https://testserver") as client:
        signup = client.post(
            "/api/auth/signup",
            json={
                "email": "versioned@example.com",
                "password": "correct-horse-battery",
                "accept_terms": True,
                "acknowledge_privacy": True,
            },
        )
        assert signup.status_code == 201
        assert signup.json()["legal_acceptance_current"] is True

    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == "versioned@example.com").one()
        rows = {r.document_type: r for r in session.query(LegalAcceptance).filter(LegalAcceptance.customer_id == customer.id)}
        assert set(rows) == {"terms", "privacy"}
        assert rows["terms"].version == TERMS_VERSION
        assert rows["privacy"].version == PRIVACY_VERSION
        assert rows["terms"].accepted_at is not None
        assert rows["privacy"].accepted_at is not None


# --- existing customers without acceptance: locked out, then restored ---


def test_existing_customer_without_acceptance_cannot_reach_account_endpoints():
    with TestClient(app, base_url="https://testserver") as client:
        _create_preexisting_customer_without_consent("legacy1@example.com", "correct-horse-battery")
        login = client.post("/api/auth/login", json={"email": "legacy1@example.com", "password": "correct-horse-battery"})
        assert login.status_code == 200
        assert login.json()["legal_acceptance_current"] is False

        get_blocked = client.get("/api/account")
        assert get_blocked.status_code == 403
        assert "ACCEPTANCE_REQUIRED" in get_blocked.json()["detail"]

        create_blocked = client.post(
            "/api/account",
            json={
                "pair": "BTC/USDT",
                "market_type": "spot",
                "notification_channel": "telegram",
                "api_key": "k",
                "api_secret": "s",
            },
        )
        assert create_blocked.status_code == 403
        assert "ACCEPTANCE_REQUIRED" in create_blocked.json()["detail"]


def test_accepting_legal_restores_access_for_existing_customer():
    with TestClient(app, base_url="https://testserver") as client:
        _create_preexisting_customer_without_consent("legacy2@example.com", "correct-horse-battery")
        client.post("/api/auth/login", json={"email": "legacy2@example.com", "password": "correct-horse-battery"})

        still_blocked = client.get("/api/account")
        assert still_blocked.status_code == 403

        accepted = client.post("/api/auth/accept-legal", json={"accept_terms": True, "acknowledge_privacy": True})
        assert accepted.status_code == 200
        assert accepted.json()["legal_acceptance_current"] is True

        now_allowed = client.get("/api/account")
        assert now_allowed.status_code == 404  # no account yet, but no longer ACCEPTANCE_REQUIRED

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


def test_accept_legal_rejects_false_flags_and_is_idempotent_when_true():
    with TestClient(app, base_url="https://testserver") as client:
        _create_preexisting_customer_without_consent("legacy3@example.com", "correct-horse-battery")
        client.post("/api/auth/login", json={"email": "legacy3@example.com", "password": "correct-horse-battery"})

        rejected = client.post("/api/auth/accept-legal", json={"accept_terms": True, "acknowledge_privacy": False})
        assert rejected.status_code == 422

        first = client.post("/api/auth/accept-legal", json={"accept_terms": True, "acknowledge_privacy": True})
        assert first.status_code == 200
        second = client.post("/api/auth/accept-legal", json={"accept_terms": True, "acknowledge_privacy": True})
        assert second.status_code == 200  # idempotent, not a duplicate-row error


def test_payment_does_not_satisfy_the_legal_consent_gate():
    with TestClient(app, base_url="https://testserver") as client:
        _create_preexisting_customer_without_consent("legacy-paid@example.com", "correct-horse-battery")
        client.post("/api/auth/login", json={"email": "legacy-paid@example.com", "password": "correct-horse-battery"})
    _mark_paid("legacy-paid@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        client.post("/api/auth/login", json={"email": "legacy-paid@example.com", "password": "correct-horse-battery"})
        response = client.get("/api/account")
        assert response.status_code == 403  # paid, but still no Terms/Privacy acceptance on file


# --- risk disclosure: a fifth, additional gate on enable-live-trading ---


def test_enable_live_trading_rejects_missing_or_false_risk_disclosure():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "noriskflag@example.com")
        _mark_paid("noriskflag@example.com")
        _retag_mainnet(client)
        _make_eligible("noriskflag@example.com")

        missing = client.post("/api/account/enable-live-trading", json={})
        assert missing.status_code == 422

        false_flag = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": False})
        assert false_flag.status_code == 422

    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == "noriskflag@example.com").one()
        assert customer.account.live_trading_enabled_at is None
        rows = session.query(LegalAcceptance).filter(
            LegalAcceptance.customer_id == customer.id, LegalAcceptance.document_type == "risk_disclosure"
        ).all()
        assert rows == []


def test_enable_live_trading_records_risk_disclosure_acceptance_only_on_success():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "riskok@example.com")
        _mark_paid("riskok@example.com")
        _retag_mainnet(client)
        _make_eligible("riskok@example.com")

        response = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert response.status_code == 200
        # Enabling live trading authorizes going live; it never starts
        # anything by itself (see onboarding.resolve_trading_mode's fifth
        # gate) - this test is only about the acceptance row below.
        assert response.json()["mode"] == "paper"

    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == "riskok@example.com").one()
        row = session.query(LegalAcceptance).filter(
            LegalAcceptance.customer_id == customer.id, LegalAcceptance.document_type == "risk_disclosure"
        ).one()
        assert row.version == RISK_DISCLOSURE_VERSION
        assert row.accepted_at is not None
        assert customer.account.live_trading_enabled_at is not None


def test_risk_disclosure_acceptance_alone_cannot_activate_live_trading():
    """Accepting the risk disclosure is additional, never a substitute,
    for the four existing gates (paid, eligible, enabled, mainnet
    credential). This account is missing every one of them except the
    credential is still the signup-default testnet tag, so the request
    must fail and - critically - must NOT leave a risk_disclosure
    acceptance row behind for an activation that never happened (the
    transactional "write together only on success" requirement)."""
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "riskonly@example.com")
        # Deliberately not paid, not eligible, not retagged to mainnet.
        response = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert response.status_code == 409

    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == "riskonly@example.com").one()
        assert customer.account.live_trading_enabled_at is None
        rows = session.query(LegalAcceptance).filter(
            LegalAcceptance.customer_id == customer.id, LegalAcceptance.document_type == "risk_disclosure"
        ).all()
        assert rows == []


def test_payment_alone_still_cannot_activate_live_trading_with_consent_wired_in():
    """The combined feature end-to-end: a paid, consent-accepted,
    trial-expired, mainnet-tagged account STILL requires the dedicated
    enable-live-trading call with risk_disclosure accepted - payment,
    eligibility, and the mainnet tag together are still not enough."""
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "fullflow@example.com")
        _mark_paid("fullflow@example.com")
        _retag_mainnet(client)
        _make_eligible("fullflow@example.com")

        before = client.get("/api/account").json()
        assert before["mode"] == "paper"  # every gate but the dedicated call itself is now met

        enabled = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert enabled.status_code == 200
        # Enabling live trading authorizes going live; it never starts
        # anything by itself - see onboarding.resolve_trading_mode's
        # fifth gate and test_api_e2e.py for the full Start Bot path.
        assert enabled.json()["mode"] == "paper"


# --- Testnet/Mainnet safety gates: unaffected by any of the above ---


def test_testnet_credential_still_blocks_live_trading_even_with_risk_disclosure_accepted():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "stilltestnet@example.com")
        _mark_paid("stilltestnet@example.com")
        _make_eligible("stilltestnet@example.com")
        # Never retagged to mainnet - credential is still the signup-default testnet tag.
        response = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert response.status_code == 409
        assert "mainnet" in response.json()["detail"].lower()


def test_downgrading_to_testnet_still_disables_live_trading_with_consent_wired_in():
    with TestClient(app, base_url="https://testserver") as client:
        _signup_with_account(client, "downgradeconsent@example.com")
        _mark_paid("downgradeconsent@example.com")
        _retag_mainnet(client)
        _make_eligible("downgradeconsent@example.com")
        enabled = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert enabled.status_code == 200
        client.patch("/api/account", json={"allocated_capital": 500})
        started = client.post("/api/account/start-bot", json={"confirm": True})
        assert started.json()["mode"] == "live"

        downgraded = client.put(
            "/api/account/credentials",
            json={"api_key": "test-key", "api_secret": "test-secret", "environment": "testnet"},
        )
        assert downgraded.status_code == 200
        assert downgraded.json()["live_trading_enabled_at"] is None
        assert downgraded.json()["mode"] == "paper"


# --- no secrets ever land in a legal acceptance record ---


def test_legal_acceptance_rows_never_contain_credentials_or_secrets():
    secret_api_key = "super-secret-mainnet-api-key-12345"
    secret_api_secret = "super-secret-mainnet-api-secret-67890"
    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/api/auth/signup",
            json={
                "email": "nosecrets@example.com",
                "password": "correct-horse-battery",
                "accept_terms": True,
                "acknowledge_privacy": True,
            },
        )
        _mark_paid("nosecrets@example.com")  # storing a mainnet credential now requires payment first
        created = client.post(
            "/api/account",
            json={
                "pair": "BTC/USDT",
                "market_type": "spot",
                "notification_channel": "telegram",
                "api_key": secret_api_key,
                "api_secret": secret_api_secret,
                "environment": "mainnet",
            },
        )
        assert created.status_code == 201
        _make_eligible("nosecrets@example.com")
        enabled = client.post("/api/account/enable-live-trading", json={"accept_risk_disclosure": True})
        assert enabled.status_code == 200

    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == "nosecrets@example.com").one()
        rows = session.query(LegalAcceptance).filter(LegalAcceptance.customer_id == customer.id).all()
        assert len(rows) == 3  # terms, privacy, risk_disclosure
        # The model itself only has these columns - no api_key/api_secret
        # field exists to leak through in the first place.
        column_names = {c.name for c in LegalAcceptance.__table__.columns}
        assert column_names == {"id", "customer_id", "document_type", "version", "accepted_at"}
        for row in rows:
            for value in (row.id, row.customer_id, row.document_type, row.version):
                assert secret_api_key not in str(value)
                assert secret_api_secret not in str(value)
