"""A missing or out-of-date consent can never stop the engine from managing an open live trade.

Consent (Terms, Privacy, Risk Disclosure) gates the customer's API and screens only. The scheduler, trader, lifecycle, reconciler and the exchange-side stop never read it. These tests prove it three ways: a
source scan (no engine module imports the legal module or the acceptance table), a live Futures trade managed to its exit, its stop and its reconciliation under every consent state, and the same trade with
the customer's consent rows absent or old.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers_live import (BEAR_PRICE, BEARISH_OHLCV, all_trades, futures_client, install, make_settings, new_account, run_cycle, run_reconcile, the_trade)

from mba_bot import legal
from mba_bot.db import init_db, session_scope
from mba_bot.models import Customer, LegalAcceptance, TradingAccount

SRC = Path(__file__).resolve().parents[1] / "src" / "mba_bot"
OLD = "2026-10-18"


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path}/test.db")


def _customer_id(account_id: str) -> str:
    with session_scope() as s:
        return s.get(TradingAccount, account_id).customer_id


def _set_consent(account_id: str, state: str) -> None:
    """none: no acceptance row at all. old: every document accepted at the PREVIOUS version only. current: all three accepted at the current version."""
    cid = _customer_id(account_id)
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        s.query(LegalAcceptance).filter(LegalAcceptance.customer_id == cid).delete()
        versions = {"none": {}, "old": {"terms": OLD, "privacy": OLD, "risk_disclosure": OLD},
                    "current": {"terms": legal.TERMS_VERSION, "privacy": legal.PRIVACY_VERSION, "risk_disclosure": legal.RISK_DISCLOSURE_VERSION}}[state]
        for doc, version in versions.items():
            s.add(LegalAcceptance(customer_id=cid, document_type=doc, version=version, accepted_at=now))


def _gate_open(account_id: str) -> bool:
    with session_scope() as s:
        return legal.legal_acceptance_current(s, customer_id=_customer_id(account_id))


def _live_trade(monkeypatch, state: str):
    settings = make_settings(max_notional_multiplier=100.0)
    client = futures_client()
    install(monkeypatch, client)
    account_id = new_account(settings, market_type="futures")
    run_cycle(account_id, settings)  # the entry
    trade = the_trade(account_id)
    assert trade.status == "open" and trade.protection_status == "protected" and not trade.is_paper
    _set_consent(account_id, state)  # consent changes AFTER the trade is open: the version was bumped, the customer has not accepted
    return client, settings, account_id


@pytest.mark.parametrize("state, gated", [("none", True), ("old", True), ("current", False)])
def test_the_consent_state_gates_the_customer_but_the_trade_is_managed_to_its_exit(db, monkeypatch, state, gated):
    client, settings, account_id = _live_trade(monkeypatch, state)
    assert _gate_open(account_id) is (not gated)  # the customer's API/screens are gated exactly when consent is missing or old

    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE  # the strategy exit comes while the consent is missing
    run_cycle(account_id, settings)

    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "crossover_exit"
    assert client.position == 0.0 and client.violations == []
    assert client.orders_by_role("stop")[0]["status"] == "canceled"  # the protective stop was cancelled by the managed exit, not left behind


@pytest.mark.parametrize("state", ["none", "old"])
def test_the_exchange_stop_firing_is_recorded_without_consent(db, monkeypatch, state):
    client, settings, account_id = _live_trade(monkeypatch, state)
    assert not _gate_open(account_id)
    client.move_price(40.0)  # through the 43.0 stop
    run_cycle(account_id, settings)
    trade = the_trade(account_id)
    assert trade.status == "closed" and trade.close_reason == "exchange_stop_triggered"


@pytest.mark.parametrize("state", ["none", "old"])
def test_the_reconciler_and_repeated_rounds_keep_the_protected_trade_untouched_without_consent(db, monkeypatch, state):
    client, settings, account_id = _live_trade(monkeypatch, state)
    stop = client.orders_by_role("stop")[0]["id"]
    for _ in range(3):
        run_cycle(account_id, settings)
        run_reconcile(account_id, settings)
    (trade,) = all_trades(account_id)
    assert trade.status == "open" and trade.protection_status == "protected"
    assert client.orders_by_role("stop")[0]["id"] == stop and client.orders_by_role("stop")[0]["status"] != "canceled"
    assert client.position != 0.0 and client.violations == []


def test_no_engine_module_reads_consent():
    """The legal module and the acceptance table are imported by the API layer and by tools that only read; never by the trading engine."""
    allowed = {"legal.py", "models.py", "data_requests.py"}  # the module itself, the table definition, the read-only operator export
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        if rel.parts[0] == "api" or rel.name in allowed:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = set()
            if isinstance(node, ast.ImportFrom):
                names = {node.module or ""} | {a.name for a in node.names}
            elif isinstance(node, ast.Import):
                names = {a.name for a in node.names}
            elif isinstance(node, ast.Attribute | ast.Name):
                names = {getattr(node, "attr", getattr(node, "id", ""))}
            if names & {"legal", "LegalAcceptance", "legal_acceptance_current", "has_current_acceptance", "risk_acceptance_needed"}:
                offenders.append(str(rel))
    assert offenders == []


def test_an_unaccepted_customer_still_gets_notifications_and_the_engine_ignores_the_gate_flag(db, monkeypatch):
    client, settings, account_id = _live_trade(monkeypatch, "none")
    client._ohlcv, client.price = BEARISH_OHLCV, BEAR_PRICE
    notifier = run_cycle(account_id, settings)
    assert any("crossover exit" in t for t in notifier.texts)  # the customer is still told what happened to the trade
