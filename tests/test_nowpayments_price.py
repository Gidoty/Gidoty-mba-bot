"""NOWPAYMENTS_PRICE_USD fails closed: a malformed, zero or negative value never turns into a silent 10-dollar invoice; the parsed price is logged when an invoice is created.

Reuses the fake NOWPayments of tests/test_nowpayments.py (no network, no key)."""
from __future__ import annotations

import logging

import pytest
from test_nowpayments import app, customer, fake, tg  # noqa: F401
from test_security_hardening import make_app  # noqa: F401

from mba_bot.billing import nowpayments as npay
from mba_bot.config import _price_from_env, load_settings
from mba_bot.db import session_scope
from mba_bot.models import BillingPayment


@pytest.mark.parametrize("raw, parsed", [(None, "10"), ("", "10"), ("  ", "10"), ("3", "3"), (" 3.50 ", "3.50"), ("0.5", "0.5"),
                                         ("abc", ""), ("$3", ""), ("3 USD", ""), ("3,00", ""), ("0", ""), ("0.00", ""), ("-3", ""), ("NaN", ""), ("Infinity", "")])
def test_price_parsing_unset_is_the_default_and_anything_unusable_is_empty_never_10(raw, parsed):
    assert _price_from_env(raw) == parsed


@pytest.mark.parametrize("bad", ["abc", "$3", "3,00", "0", "-3", "NaN"])
def test_a_malformed_price_refuses_the_invoice_with_an_operator_line_and_creates_nothing(app, fake, caplog, bad):
    with app(NOWPAYMENTS_PRICE_USD=bad) as c:
        customer(c)
        assert load_settings().nowpayments_price_usd == ""
        with caplog.at_level(logging.INFO):
            r = c.post("/api/billing/crypto/initialize")
        assert r.status_code == 503 and "price" not in r.text.lower()  # the customer only learns that crypto payment is not available
        assert fake.posts == []  # no invoice was requested
        with session_scope() as s:
            assert s.query(BillingPayment).count() == 0
        line = next(r_.getMessage() for r_ in caplog.records if "NOWPAYMENTS_PRICE_USD is malformed, zero or negative" in r_.getMessage())
        assert "refusing to create an invoice" in line and bad not in line  # says what is wrong and what to do; no value dumped
        options = c.get("/api/billing/options").json()
        assert options["crypto"]["available"] is False  # no crypto button while the price is invalid


def test_the_options_route_does_not_spam_the_operator_line(app, fake, caplog):
    with app(NOWPAYMENTS_PRICE_USD="oops") as c:
        customer(c)
        with caplog.at_level(logging.INFO):
            for _ in range(3):
                assert c.get("/api/billing/options").status_code == 200
        assert not [r for r in caplog.records if "NOWPAYMENTS_PRICE_USD" in r.getMessage()]


def test_a_valid_price_is_used_exactly_and_logged_when_the_invoice_is_created(app, fake, caplog):
    with app(NOWPAYMENTS_PRICE_USD="3") as c:
        customer(c)
        with caplog.at_level(logging.INFO):
            assert c.post("/api/billing/crypto/initialize").status_code == 200
        assert fake.posts[0]["price_amount"] == 3.0
        with session_scope() as s:
            assert s.query(BillingPayment).one().amount_minor == 300  # not 1000
        assert any("creating an invoice for price_usd=3" in r.getMessage() for r in caplog.records)


def test_an_unset_price_is_still_the_documented_default_and_is_logged(app, fake, caplog, monkeypatch):
    monkeypatch.delenv("NOWPAYMENTS_PRICE_USD", raising=False)
    with app() as c:
        customer(c)
        with caplog.at_level(logging.INFO):
            assert c.post("/api/billing/crypto/initialize").status_code == 200
        with session_scope() as s:
            assert s.query(BillingPayment).one().amount_minor == 1000
        assert any("price_usd=10" in r.getMessage() for r in caplog.records)  # visible in the journal, so a forgotten variable is noticed


def test_price_cents_never_guesses():
    class S:
        nowpayments_price_usd = ""

    with pytest.raises(npay.NotConfigured):
        npay.price_cents(S())
