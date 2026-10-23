"""The read-only fee/funding audit: the boundary is tested as hard as the feature.

Nothing here touches the network. A recording transport stands in for the exchange, so we can prove which requests would have
been sent, and that refused ones never were."""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import ccxt.async_support as ccxt_async
import pytest

from mba_bot import readonly_audit as ra
from mba_bot.crypto_utils import encrypt, generate_key
from mba_bot.models import ApiCredential, Base, Customer, TradingAccount
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SRC = Path(ra.__file__)
API_KEY, API_SECRET = "TESTKEY-abcdef0123456789", "TESTSECRET-9876543210fedcba"


class Recorder:
    """Replaces the transport underneath the guard."""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, str]] = []
        self.responses = responses or {}

    async def __call__(self, url, method="GET", headers=None, body=None):
        self.calls.append((method, url))
        path = re.sub(r"^https://[^/]+", "", url).split("?")[0]
        return self.responses.get(path, [])


def make_client(rec: Recorder) -> ra.ReadOnlyBinance:
    c = ccxt_async.binanceusdm({"apiKey": API_KEY, "secret": API_SECRET, "enableRateLimit": False})
    c.fetch = rec  # the guard wraps whatever sits at the chokepoint
    return ra.ReadOnlyBinance(c)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ the allow-list


@pytest.mark.parametrize("url,method,ok", [
    ("https://fapi.binance.com/fapi/v1/commissionRate?symbol=BTCUSDT&timestamp=1&signature=x", "GET", True),
    ("https://fapi.binance.com/fapi/v1/income?incomeType=FUNDING_FEE", "GET", True),
    ("https://fapi.binance.com/fapi/v1/userTrades?symbol=BTCUSDT", "GET", True),
    ("https://fapi.binance.com/fapi/v1/feeBurn", "GET", True),
    ("https://fapi.binance.com/fapi/v2/account", "GET", True),
    ("https://fapi.binance.com/fapi/v1/commissionRate", "POST", False),
    ("https://fapi.binance.com/fapi/v1/income", "DELETE", False),
    ("https://fapi.binance.com/fapi/v1/order", "GET", False),
    ("https://fapi.binance.com/fapi/v1/order", "POST", False),
    ("https://fapi.binance.com/fapi/v1/order", "DELETE", False),
    ("https://fapi.binance.com/fapi/v1/allOpenOrders", "DELETE", False),
    ("https://fapi.binance.com/fapi/v1/leverage", "POST", False),
    ("https://fapi.binance.com/fapi/v1/marginType", "POST", False),
    ("https://fapi.binance.com/fapi/v1/positionSide/dual", "POST", False),
    ("https://fapi.binance.com/fapi/v1/listenKey", "POST", False),
    ("https://fapi.binance.com/fapi/v1/feeBurn", "POST", False),  # the write twin of an allowed read
    ("https://api.binance.com/sapi/v1/capital/withdraw/apply", "POST", False),
    ("https://api.binance.com/sapi/v1/asset/transfer", "POST", False),
    ("https://api.binance.com/sapi/v1/futures/transfer", "POST", False),
    ("https://api.binance.com/api/v3/order", "POST", False),
    ("https://fapi.binance.com/fapi/v1/income/../order", "GET", False),
    ("https://fapi.binance.com.evil.example/fapi/v1/income", "GET", False),
    ("https://demo-fapi.binance.com/fapi/v1/income", "GET", False),
    ("http://fapi.binance.com/fapi/v1/income", "GET", False),
])
def test_only_get_requests_to_the_allow_listed_paths_pass(url, method, ok):
    assert ra.request_allowed(url, method) is ok


def test_the_guard_refuses_before_sending_anything():
    rec = Recorder()
    c = ccxt_async.binanceusdm({"apiKey": API_KEY, "secret": API_SECRET})
    c.fetch = rec
    ra.install_guard(c)
    for call in (lambda: c.fapiPrivatePostOrder({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": 1}),
                 lambda: c.fapiPrivateDeleteAllOpenOrders({"symbol": "BTCUSDT"}),
                 lambda: c.fapiPrivatePostLeverage({"symbol": "BTCUSDT", "leverage": 20}),
                 lambda: c.fapiPrivatePostMarginType({"symbol": "BTCUSDT", "marginType": "ISOLATED"}),
                 lambda: c.sapiPostCapitalWithdrawApply({"coin": "USDT", "address": "x", "amount": 1}),
                 lambda: c.sapiPostAssetTransfer({"type": "MAIN_UMFUTURE", "asset": "USDT", "amount": 1})):
        with pytest.raises(ra.ReadOnlyViolation):
            run(call())
    assert rec.calls == []  # not one request reached the transport


@pytest.mark.parametrize("name,call", [
    ("create_order", lambda c: c.create_order("BTC/USDT:USDT", "market", "buy", 0.001)),
    ("cancel_order", lambda c: c.cancel_order("1", "BTC/USDT:USDT")),
    ("cancel_all_orders", lambda c: c.cancel_all_orders("BTC/USDT:USDT")),
    ("set_leverage", lambda c: c.set_leverage(10, "BTC/USDT:USDT")),
    ("set_margin_mode", lambda c: c.set_margin_mode("isolated", "BTC/USDT:USDT")),
    ("transfer", lambda c: c.transfer("USDT", 1, "future", "spot")),
    ("withdraw", lambda c: c.withdraw("USDT", 1, "address")),
])
def test_the_unified_trading_methods_cannot_get_through_either(name, call):
    rec = Recorder()
    c = ccxt_async.binanceusdm({"apiKey": API_KEY, "secret": API_SECRET})  # a fresh client per call: ccxt keeps a pending market-load future after a failure
    c.fetch = rec
    ra.install_guard(c)
    with pytest.raises(ra.ReadOnlyViolation):
        run(asyncio.wait_for(call(c), 10))
    assert not any(m != "GET" for m, _ in rec.calls)
    assert all(ra.request_allowed(u, m) for m, u in rec.calls)


def test_allowed_reads_go_through_signed_and_unchanged():
    rec = Recorder({"/fapi/v1/commissionRate": {"symbol": "BTCUSDT", "makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"},
                    "/fapi/v2/account": {"feeTier": 0, "canTrade": True, "multiAssetsMargin": False}, "/fapi/v1/feeBurn": {"feeBurn": True}})
    ro = make_client(rec)
    assert run(ro.commission_rate("BTCUSDT")) == {"symbol": "BTCUSDT", "maker": 0.0002, "taker": 0.0005}
    assert run(ro.account_fee_tier())["fee_tier"] == 0 and run(ro.fee_burn_enabled()) is True
    assert rec.calls and all(m == "GET" for m, _ in rec.calls)
    assert all("signature=" in u for _, u in rec.calls)  # private reads are signed by ccxt


def test_a_body_is_refused_even_on_an_allowed_path():
    rec = Recorder()
    c = ccxt_async.binanceusdm({"apiKey": API_KEY, "secret": API_SECRET})
    c.fetch = rec
    ra.install_guard(c)
    with pytest.raises(ra.ReadOnlyViolation):
        run(c.fetch("https://fapi.binance.com/fapi/v1/income", "GET", {}, "symbol=BTCUSDT"))
    assert rec.calls == []


# ------------------------------------------------------------------ the surface and the source


def test_the_public_surface_is_exactly_the_five_reads_and_close():
    public = {n for n in dir(ra.ReadOnlyBinance) if not n.startswith("_")}
    assert public == {"commission_rate", "account_fee_tier", "fee_burn_enabled", "income", "user_trades", "close"}
    ro = make_client(Recorder())
    with pytest.raises(AttributeError):
        ro.create_order  # noqa: B018
    with pytest.raises(AttributeError):
        ro.anything = 1
    with pytest.raises(ValueError):
        run(ro.income("REALIZED_PNL", 0, 1))  # only the two income types the audit needs


FORBIDDEN = ("create_order", "cancel_order", "cancel_all", "withdraw", "transfer", "set_leverage", "set_margin", "set_position_mode", "fapiPrivatePost", "fapiPrivateDelete",
             "fapiPrivatePut", "sapiPost", "sapiDelete", "sapiPut", "apiPost", "private_post", "private_delete", "private_put", "listenKey", "edit_order", "Trader", "place_")


def test_the_source_contains_no_trading_money_movement_or_write_vocabulary():
    text = SRC.read_text()
    tree = ast.parse(text)
    doc_free = ast.unparse(tree).replace(ast.get_docstring(tree) or "", "")
    for token in FORBIDDEN:
        assert token not in doc_free, f"forbidden vocabulary in the read-only tool: {token}"


def test_the_module_imports_none_of_the_trading_code():
    imported = set()
    for node in ast.walk(ast.parse(SRC.read_text())):
        if isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            imported.update(f"{'.' * node.level}{node.module or ''}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = ("trader", "scheduler", "lifecycle", "risk", "venue", "telemetry", "onboarding", "api", "db", "main", "notifications", "research")
    for name in imported:
        for b in banned:
            assert not re.search(rf"(^|\.){b}(\.|$)", name), f"{name} pulls in {b}"


def test_nothing_in_the_service_imports_the_audit_tool():
    root = SRC.parent
    for path in root.rglob("*.py"):
        if path == SRC:
            continue
        assert "readonly_audit" not in path.read_text(), f"{path.name} references the read-only audit"


# ------------------------------------------------------------------ credentials: same path as the service, read-only, no leaks


@pytest.fixture
def db(tmp_path):
    key = generate_key()
    path = tmp_path / "mba.db"
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    with S() as s:
        cust = Customer(email="owner@example.com", password_hash="x")
        s.add(cust)
        s.flush()
        acct = TradingAccount(customer_id=cust.id, pair="BTC/USDT", market_type="futures", notification_channel="telegram", bot_code="MBA-TEST01", trial_ends_at=datetime.now(timezone.utc))
        s.add(acct)
        s.flush()
        s.add(ApiCredential(account_id=acct.id, exchange="binance", environment="mainnet", encrypted_api_key=encrypt(API_KEY, key=key), encrypted_api_secret=encrypt(API_SECRET, key=key)))
        cust2 = Customer(email="t@example.com", password_hash="x")
        s.add(cust2)
        s.flush()
        acct2 = TradingAccount(customer_id=cust2.id, pair="BTC/USDT", market_type="futures", notification_channel="telegram", bot_code="MBA-TEST02", trial_ends_at=datetime.now(timezone.utc))
        s.add(acct2)
        s.flush()
        s.add(ApiCredential(account_id=acct2.id, exchange="binance", environment="testnet", encrypted_api_key=encrypt(API_KEY, key=key), encrypted_api_secret=encrypt(API_SECRET, key=key)))
        s.commit()
        ids = (acct.id, acct2.id)
    eng.dispose()
    return {"path": path, "key": key, "ids": ids}


def test_the_credential_loads_through_the_services_decrypt_path(db):
    k, s, facts = ra.load_credential(db["path"], "MBA-TEST01", encryption_key=db["key"])
    assert (k, s) == (API_KEY, API_SECRET) and facts["environment"] == "mainnet" and "key" not in json.dumps(facts).lower()
    assert ra.load_credential(db["path"], db["ids"][0], encryption_key=db["key"])[2]["bot_code"] == "MBA-TEST01"


def test_it_refuses_a_non_mainnet_unknown_or_ambiguous_account_and_a_wrong_key(db):
    with pytest.raises(ra.AuditInputError, match="mainnet"):
        ra.load_credential(db["path"], "MBA-TEST02", encryption_key=db["key"])
    with pytest.raises(ra.AuditInputError, match="exactly one"):
        ra.load_credential(db["path"], "MBA-NOPE", encryption_key=db["key"])
    with pytest.raises(Exception) as e:
        ra.load_credential(db["path"], "MBA-TEST01", encryption_key=generate_key())
    assert API_KEY not in str(e.value) and API_SECRET not in str(e.value)


def test_the_database_is_opened_read_only_and_is_byte_identical_afterwards(db):
    before = db["path"].read_bytes()
    con = ra.open_readonly(db["path"])
    with pytest.raises(sqlite3.OperationalError):
        con.execute("UPDATE trading_accounts SET pair = 'X'")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM api_credentials")
    con.close()
    ra.load_credential(db["path"], "MBA-TEST01", encryption_key=db["key"])
    assert db["path"].read_bytes() == before


def test_a_leak_check_blocks_any_output_containing_a_credential():
    assert ra.assert_clean("fee 5 bp", (API_KEY, API_SECRET)) == "fee 5 bp"
    for text in (f"key {API_KEY}", f"x{API_SECRET}y"):
        with pytest.raises(ra.LeakDetected):
            ra.assert_clean(text, (API_KEY, API_SECRET))


def test_the_cli_never_prints_a_credential_even_when_the_exchange_error_contains_one(db, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("ENCRYPTION_KEY", db["key"])

    class Boom(Recorder):
        async def __call__(self, url, method="GET", headers=None, body=None):
            raise ccxt_async.ExchangeError(f"binance {{\"msg\":\"Invalid key {API_KEY} secret {API_SECRET}\"}}")

    def fake_build(key, secret):
        assert (key, secret) == (API_KEY, API_SECRET)
        return make_client(Boom())

    monkeypatch.setattr(ra, "build_client", fake_build)
    code = ra.main(["--database", str(db["path"]), "--account-id", "MBA-TEST01", "--days", "7"])
    cap = capsys.readouterr()
    assert code == 0  # reads failed softly; the report says so by category
    for blob in (cap.out, cap.err, caplog.text):
        assert API_KEY not in blob and API_SECRET not in blob
    assert "Some reads failed" in cap.out and "Binance rejected a request" in cap.out


def test_the_cli_reports_a_refusal_without_a_stack_trace(db, monkeypatch, capsys):
    monkeypatch.setenv("ENCRYPTION_KEY", db["key"])

    class Sneaky:
        async def commission_rate(self, symbol):
            raise ra.ReadOnlyViolation("x")

        async def close(self):
            return None

    monkeypatch.setattr(ra, "build_client", lambda k, s: Sneaky())
    assert ra.main(["--database", str(db["path"]), "--account-id", "MBA-TEST01"]) == 4
    assert "nothing was sent" in capsys.readouterr().err


# ------------------------------------------------------------------ the analysis


def test_fees_and_funding_are_summarised_per_symbol_without_converting_other_assets():
    fills = [{"symbol": "BTCUSDT", "quoteQty": "1000", "commission": "0.5", "commissionAsset": "USDT", "maker": False},
             {"symbol": "BTCUSDT", "quoteQty": "1000", "commission": "0.2", "commissionAsset": "USDT", "maker": True},
             {"symbol": "ETHUSDT", "quoteQty": "500", "commission": "0.001", "commissionAsset": "BNB", "maker": False}]
    f = ra.summarize_fills(fills)
    assert f["by_symbol"]["BTCUSDT"]["effective_fee_bps"] == pytest.approx(3.5) and f["by_symbol"]["BTCUSDT"]["effective_taker_bps"] == pytest.approx(5.0)
    assert f["by_symbol"]["ETHUSDT"]["non_usdt_fills"] == 1 and f["by_symbol"]["ETHUSDT"]["fee_usdt"] == 0
    inc = ra.summarize_income([{"symbol": "BTCUSDT", "income": "-0.5", "asset": "USDT"}, {"symbol": "ETHUSDT", "income": "-0.001", "asset": "BNB"}],
                              [{"symbol": "BTCUSDT", "income": "-0.12", "asset": "USDT"}, {"symbol": "BTCUSDT", "income": "0.03", "asset": "USDT"}])
    assert inc["total_commission_usdt"] == pytest.approx(-0.5) and inc["commission_in_other_assets"] == {"BNB": -0.001}
    assert inc["total_funding_usdt"] == pytest.approx(-0.09) and inc["by_symbol"]["BTCUSDT"]["funding_events"] == 2
    cmp_ = ra.compare_to_model(f, {"BTCUSDT": {"taker": 0.0004}})
    assert cmp_["stated_over_model"] == pytest.approx(0.8) and cmp_["realised_over_model"] == pytest.approx(1.0)


def test_a_full_audit_run_pages_income_and_only_issues_reads():
    page = [{"symbol": "BTCUSDT", "income": "-0.1", "asset": "USDT", "time": str(1_000 + i)} for i in range(1000)]
    rec = Recorder({"/fapi/v1/commissionRate": {"symbol": "BTCUSDT", "makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"},
                    "/fapi/v2/account": {"feeTier": 1}, "/fapi/v1/feeBurn": {"feeBurn": False}, "/fapi/v1/income": page, "/fapi/v1/userTrades": []})
    res = run(ra.run_audit(make_client(rec), ["BTCUSDT"], days=3, now_ms=10_000_000_000))
    assert res["commission_rates"]["BTCUSDT"]["taker"] == 0.0005 and res["errors"] == {} and res["fee_burn_enabled"] is False
    assert all(m == "GET" for m, _ in rec.calls) and all(ra.request_allowed(u, m) for m, u in rec.calls)
    assert "Read-only fee and funding audit" in ra.render(res, {"bot_code": "MBA-TEST01", "market_type": "futures", "environment": "mainnet"})
