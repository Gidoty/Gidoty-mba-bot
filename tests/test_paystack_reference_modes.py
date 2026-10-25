"""Paystack follow-up (branch security/step2-paystack-reference):

1. /initialize no longer makes an earlier checkout link unpayable under enforcement: every issued reference is recorded.
2. PAYSTACK_STRICT_VERIFY=off|log|enforce. LOG runs the whole verification and only writes a log line: it must never
   change what is accepted or rejected, and its log must hold no secret, email or raw payload.

No network: Paystack is a stub, the scheduler an idle task."""
from __future__ import annotations

import asyncio
import logging

import pytest
from test_security_hardening import AMOUNT, SK, STRICT, charge, client_for, make_app, new_customer, paystack, post_event, verified  # noqa: F401

from mba_bot.api.routers import billing
from mba_bot.config import parse_verify_mode, verify_mode_is_unrecognised
from mba_bot.db import session_scope
from mba_bot.models import Customer, PaymentEvent, PaymentReference

SIGNUP = {"password": "correct-horse-battery", "accept_terms": True, "acknowledge_privacy": True}


class _Resp:
    is_error = False

    def __init__(self, reference):
        self._ref = reference

    def json(self):
        return {"status": True, "data": {"reference": self._ref, "authorization_url": f"https://checkout.paystack.test/{self._ref}"}}


def issue_references(monkeypatch, refs):
    it = iter(refs)
    monkeypatch.setattr(billing.httpx, "post", lambda *a, **k: _Resp(next(it)))


def signed_up(client, email="payer@example.com") -> str:
    r = client.post("/api/auth/signup", json={"email": email, **SIGNUP})
    assert r.status_code == 201
    return r.json()["id"]


def user(cid):
    """(has_paid, account active or None, stored reference); works for customers that have no trading account yet."""
    with session_scope() as s:
        c = s.get(Customer, cid)
        return c.has_paid, (c.account.is_active if c.account is not None else None), c.paystack_reference


def rows(model):
    with session_scope() as s:
        return s.query(model).count()


# ============================================================ the setting

@pytest.mark.parametrize("raw,mode", [(None, "off"), ("", "off"), ("0", "off"), ("false", "off"), ("off", "off"), ("No", "off"),
                                      ("1", "enforce"), ("true", "enforce"), ("YES", "enforce"), ("on", "enforce"), ("enforce", "enforce"), (" Enforce ", "enforce"),
                                      ("log", "log"), ("LOG", "log"),
                                      ("enforced", "off"), ("2", "off"), ("lo g", "off"), ("maybe", "off")])
def test_the_mode_parser_never_enforces_by_accident(raw, mode):
    assert parse_verify_mode(raw) == mode
    assert verify_mode_is_unrecognised(raw) is (raw is not None and raw.strip().lower() not in ("", "0", "false", "no", "off", "1", "true", "yes", "on", "enforce", "log"))


def test_only_enforce_counts_as_strict(monkeypatch):
    from dataclasses import replace

    from mba_bot.config import load_settings
    base = load_settings()
    assert [replace(base, paystack_verify_mode=m).paystack_strict_verify for m in ("off", "log", "enforce")] == [False, False, True]


def test_an_unrecognised_value_is_reported_at_startup_and_treated_as_off(make_app, caplog):
    caplog.set_level(logging.WARNING)
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="enforced")):
        pass
    assert "unrecognised value" in caplog.text and "treating it as OFF" in caplog.text


# ============================================================ issued references

def test_with_the_mode_off_initialize_behaves_exactly_as_before(make_app, monkeypatch):
    issue_references(monkeypatch, ["ref_A", "ref_B"])
    with client_for(make_app()) as c:
        cid = signed_up(c)
        assert c.post("/api/billing/initialize").json()["authorization_url"].endswith("ref_A")
        c.post("/api/billing/initialize")
        assert user(cid)[2] == "ref_B"  # the latest reference overwrites, as today
        assert rows(PaymentReference) == 0  # nothing new is written


@pytest.mark.parametrize("mode", ["log", "enforce"])
def test_log_and_enforce_record_every_reference_and_keep_the_old_column(make_app, monkeypatch, mode):
    issue_references(monkeypatch, ["ref_A", "ref_B"])
    with client_for(make_app(PAYSTACK_STRICT_VERIFY=mode)) as c:
        cid = signed_up(c)
        c.post("/api/billing/initialize")
        c.post("/api/billing/initialize")
        assert user(cid)[2] == "ref_B"  # the column behaves as before
        with session_scope() as s:
            got = {(r.reference, r.customer_id, r.amount, r.currency) for r in s.query(PaymentReference)}
        assert got == {("ref_A", cid, AMOUNT, "NGN"), ("ref_B", cid, AMOUNT, "NGN")}


def test_a_failure_to_record_never_breaks_checkout(make_app, monkeypatch):
    issue_references(monkeypatch, ["ref_A"])

    def boom(**kw):
        raise RuntimeError("db trouble")

    monkeypatch.setattr(billing, "PaymentReference", boom)
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="log")) as c:
        signed_up(c)
        r = c.post("/api/billing/initialize")
        assert r.status_code == 200 and r.json()["authorization_url"].endswith("ref_A")


def test_enforce_accepts_payment_of_the_FIRST_link_after_starting_checkout_twice(make_app, paystack, monkeypatch):
    issue_references(monkeypatch, ["ref_A", "ref_B"])
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="enforce")) as c:
        cid = signed_up(c)
        c.post("/api/billing/initialize")
        c.post("/api/billing/initialize")
        paystack.answer = verified(cid, "ref_A")
        assert post_event(c, charge(cid, "ref_A")).status_code == 200
        assert user(cid)[0] is True and paystack.calls == ["ref_A"]
        with session_scope() as s:
            assert s.query(PaymentEvent).one().reference == "ref_A"


def test_paying_both_links_unlocks_once_and_records_both(make_app, paystack, monkeypatch):
    issue_references(monkeypatch, ["ref_A", "ref_B"])
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="enforce")) as c:
        cid = signed_up(c)
        c.post("/api/billing/initialize")
        c.post("/api/billing/initialize")
        for ref in ("ref_A", "ref_B"):
            paystack.answer = verified(cid, ref)
            post_event(c, charge(cid, ref))
        with session_scope() as s:
            assert {(e.reference, e.outcome) for e in s.query(PaymentEvent)} == {("ref_A", "applied"), ("ref_B", "already_paid")}
        assert user(cid)[0] is True


def test_references_issued_before_the_table_existed_still_work_through_the_latest_one(make_app, paystack):
    """The deploy leaves existing customers with only the old column. The latest reference keeps working; an older one
    from before the deploy cannot be recognised (it was never recorded), which is the documented limit."""
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="enforce")) as c:
        cid = new_customer(reference="ref_latest")
        paystack.answer = verified(cid, "ref_latest")
        post_event(c, charge(cid, "ref_latest"))
        assert user(cid)[0] is True
        other = new_customer("o@example.com", reference="ref_latest_o")
        paystack.answer = verified(other, "ref_older")
        post_event(c, charge(other, "ref_older"))
        assert user(other)[0] is False


def test_a_reference_issued_to_one_customer_cannot_unlock_another(make_app, paystack):
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="enforce")) as c:
        x = new_customer("x@example.com", reference="ref_x")
        y = new_customer("y@example.com", reference="ref_y")
        with session_scope() as s:
            s.add(PaymentReference(provider="paystack", reference="ref_x", customer_id=x, amount=AMOUNT, currency="NGN"))
        paystack.answer = verified(y, "ref_x")
        post_event(c, charge(y, "ref_x"))
        assert user(y)[0] is False and paystack.calls == []  # refused before asking Paystack


def test_the_price_at_the_time_of_issue_is_what_counts(make_app, paystack, monkeypatch):
    issue_references(monkeypatch, ["ref_old_price"])
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="enforce")) as c:
        cid = signed_up(c)
        c.post("/api/billing/initialize")  # issued at AMOUNT
        monkeypatch.setenv("PAYSTACK_AMOUNT", str(AMOUNT * 2))  # the owner changes the price afterwards
        paystack.answer = verified(cid, "ref_old_price")  # the customer paid what they were asked
        post_event(c, charge(cid, "ref_old_price"))
        assert user(cid)[0] is True
        # and a payment that matches only the NEW price for that old reference is not accepted
        other = new_customer("late@example.com", reference="ref_o2")
        with session_scope() as s:
            s.add(PaymentReference(provider="paystack", reference="ref_o2", customer_id=other, amount=AMOUNT, currency="NGN"))
        paystack.answer = verified(other, "ref_o2", amount=AMOUNT * 2)
        post_event(c, charge(other, "ref_o2"))
        assert user(other)[0] is False


# ============================================================ log mode never changes an outcome

def _scenarios():
    def base(cid):
        return charge(cid, "ref_1")

    return [
        ("genuine", lambda cid: (base(cid), verified(cid))),
        ("underpaid", lambda cid: (base(cid), verified(cid, amount=AMOUNT - 1))),
        ("wrong currency", lambda cid: (base(cid), verified(cid, currency="USD"))),
        ("verify says abandoned", lambda cid: (base(cid), verified(cid, status="abandoned"))),
        ("verify names another customer", lambda cid: (base(cid), verified(cid, metadata={"customer_id": "someone-else"}))),
        ("reference never issued", lambda cid: (charge(cid, "ref_unknown"), verified(cid, "ref_unknown"))),
        ("paystack unreachable", lambda cid: (base(cid), billing.PaystackUnavailable("http 503"))),
        ("paystack raises something unexpected", lambda cid: (base(cid), ValueError("odd"))),
        ("not a charge event", lambda cid: ({"event": "charge.failed", "data": {"reference": "ref_1", "metadata": {"customer_id": cid}}}, verified(cid))),
        ("event not successful", lambda cid: ({"event": "charge.success", "data": {"status": "failed", "reference": "ref_1", "metadata": {"customer_id": cid}}}, verified(cid))),
        ("unknown customer", lambda cid: (charge("nobody", "ref_1"), verified("nobody"))),
    ]


@pytest.mark.parametrize("name,build", _scenarios(), ids=[n for n, _ in _scenarios()])
def test_log_mode_gives_the_same_result_as_off_for_every_kind_of_event(make_app, paystack, tmp_path, name, build):
    results = {}
    for mode in ("off", "log"):
        with client_for(make_app(PAYSTACK_STRICT_VERIFY=mode, DATABASE_URL=f"sqlite:///{tmp_path}/{mode}.db")) as c:
            cid = new_customer()
            event, answer = build(cid)
            paystack.answer, paystack.calls = answer, []
            r = post_event(c, event)
            results[mode] = (r.status_code, r.json(), user(cid), rows(PaymentEvent), rows(PaymentReference))
    assert results["log"] == results["off"], name


def test_log_mode_logs_a_would_be_rejection_but_still_grants_as_today(make_app, paystack, caplog):
    caplog.set_level(logging.INFO)
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="log")) as c:
        cid = new_customer(reference="ref_1_long_reference_value")
        paystack.answer = verified(cid, "ref_1_long_reference_value", amount=AMOUNT - 5)
        assert post_event(c, charge(cid, "ref_1_long_reference_value")).status_code == 200
        assert user(cid)[0] is True  # today's behaviour
    line = next(r.getMessage() for r in caplog.records if "paystack verify[log]" in r.getMessage())
    assert "verdict=reject" in line and "reason=underpaid" in line and "today=grants" in line
    assert f"customer={cid[:8]}..." in line and "ref=..._value" in line


def test_log_mode_accept_and_unreachable_verdicts(make_app, paystack, caplog):
    caplog.set_level(logging.INFO)
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="log")) as c:
        cid = new_customer(reference="ref_ok")
        paystack.answer = verified(cid, "ref_ok")
        post_event(c, charge(cid, "ref_ok"))
        other = new_customer("u@example.com", reference="ref_u")
        paystack.answer = billing.PaystackUnavailable("http 503")
        assert post_event(c, charge(other, "ref_u")).status_code == 200  # never 503 in log mode
        assert user(other)[0] is True
    text = caplog.text
    assert "verdict=accept reason=ok" in text and "verdict=unavailable" in text


def test_log_mode_cannot_be_held_up_by_a_slow_paystack(make_app, paystack, monkeypatch):
    monkeypatch.setattr(billing, "_SHADOW_TIMEOUT_SECONDS", 0.05)

    async def slow(reference, settings):
        await asyncio.sleep(2)
        return verified("x")

    monkeypatch.setattr(billing, "_verify_with_paystack", slow)
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="log")) as c:
        cid = new_customer()
        assert post_event(c, charge(cid)).status_code == 200
        assert user(cid)[0] is True


def test_log_mode_writes_no_secret_email_or_raw_payload(make_app, paystack, caplog):
    caplog.set_level(logging.DEBUG)
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="log")) as c:
        cid = new_customer("private.person@example.com", reference="ref_secretish_123456")
        answer = verified(cid, "ref_secretish_123456")
        answer["data"]["authorization"] = {"card_type": "visa", "last4": "4081", "signature": "SIG_abc"}
        answer["data"]["customer"] = {"email": "private.person@example.com"}
        paystack.answer = answer
        post_event(c, charge(cid, "ref_secretish_123456"))
        paystack.answer = verified(cid, "ref_secretish_123456", amount=1)
        post_event(c, charge(cid, "ref_secretish_123456"))
    blob = caplog.text
    for forbidden in (cid, "private.person@example.com", SK, "ref_secretish_123456", "4081", "SIG_abc", "visa", "authorization", "metadata"):
        assert forbidden not in blob, forbidden


# ============================================================ enforce mode

def test_enforce_still_returns_503_when_paystack_is_unreachable_and_log_never_does(make_app, paystack, tmp_path):
    codes = {}
    for mode in ("off", "log", "enforce"):
        with client_for(make_app(PAYSTACK_STRICT_VERIFY=mode, DATABASE_URL=f"sqlite:///{tmp_path}/{mode}.db")) as c:
            cid = new_customer()
            paystack.answer = billing.PaystackUnavailable("http 503")
            codes[mode] = (post_event(c, charge(cid)).status_code, user(cid)[0])
    assert codes == {"off": (200, True), "log": (200, True), "enforce": (503, False)}


def test_the_word_enforce_and_the_number_one_are_the_same_mode(make_app, paystack, tmp_path):
    out = []
    for raw in ("1", "enforce"):
        with client_for(make_app(PAYSTACK_STRICT_VERIFY=raw, DATABASE_URL=f"sqlite:///{tmp_path}/{raw}.db")) as c:
            cid = new_customer()
            paystack.answer = verified(cid, amount=AMOUNT - 1)
            post_event(c, charge(cid))
            out.append(user(cid)[0])
    assert out == [False, False]


def test_enforce_logs_mask_the_customer_id(make_app, paystack, caplog):
    caplog.set_level(logging.WARNING)
    with client_for(make_app(PAYSTACK_STRICT_VERIFY="enforce")) as c:
        cid = new_customer(reference="ref_1")
        paystack.answer = verified(cid, amount=AMOUNT - 1)
        post_event(c, charge(cid))
    assert "reason=underpaid" in caplog.text and cid not in caplog.text and f"{cid[:8]}..." in caplog.text


# ============================================================ the table and its proposed migration

def test_the_payment_references_table_is_additive_and_holds_no_personal_data():
    cols = {c.name for c in PaymentReference.__table__.columns}
    assert cols == {"id", "provider", "reference", "customer_id", "amount", "currency", "created_at"}
    assert any(c.name == "uq_payment_references_provider_reference" for c in PaymentReference.__table__.constraints)


def test_the_proposed_references_migration_is_idempotent_reversible_and_not_installed(tmp_path):
    import importlib.util
    import re
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location("proposed2", root / "docs" / "security" / "proposed_migration_payment_references.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    live = {re.search(r'^revision[^=\n]*=\s*["\'](\w+)["\']', p.read_text(), re.M).group(1) for p in (root / "alembic" / "versions").glob("*.py")}
    assert mod.revision not in live and mod.down_revision == "e1f7a3c9b5d2"
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    with eng.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()
            assert inspect(conn).has_table("payment_references")
            mod.upgrade()
            mod.downgrade()
            assert not inspect(conn).has_table("payment_references")
