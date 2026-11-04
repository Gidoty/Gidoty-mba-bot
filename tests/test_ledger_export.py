"""The owner-run ledger export: same read-only boundary as the fee audit, sanitised output, never overwrites.

No network: a recording transport stands in for the exchange, as in test_readonly_audit."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path

import ccxt.async_support as ccxt_async
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mba_bot import ledger_export as le
from mba_bot import readonly_audit as ra
from mba_bot.crypto_utils import encrypt, generate_key
from mba_bot.models import ApiCredential, Base, Customer, Trade, TradingAccount
from mba_bot.research import ledger as lg  # tests may import both sides; the modules themselves may not

import test_readonly_audit as tra  # reuses its Recorder and forbidden-vocabulary list

SRC = Path(le.__file__)
API_KEY, API_SECRET = tra.API_KEY, tra.API_SECRET
T0 = 1_760_000_000_000


@pytest.fixture
def env(tmp_path, monkeypatch):
    key = generate_key()
    path = tmp_path / "mba.db"
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    from datetime import datetime, timedelta, timezone

    base = datetime.fromtimestamp(T0 / 1000, tz=timezone.utc)
    with S() as s:
        cust = Customer(email="owner-secret-mail@example.com", password_hash="x", has_paid=True)
        s.add(cust)
        s.flush()
        acct = TradingAccount(customer_id=cust.id, bot_code="MBA-SECRETCODE", pair="BTC/USDT", market_type="futures", notification_channel="telegram",
                              notification_target="555000111", trial_ends_at=base)
        s.add(acct)
        s.flush()
        s.add(ApiCredential(account_id=acct.id, exchange="binance", environment="mainnet",
                            encrypted_api_key=encrypt(API_KEY, key=key), encrypted_api_secret=encrypt(API_SECRET, key=key)))
        s.add(Trade(account_id=acct.id, side="long", is_paper=False, entry_price=100.0, quantity=1.0, stop_loss_price=98.0, exit_price=102.0, pnl=1.9,
                    status="closed", close_reason="crossover_exit", pair="BTC/USDT", opened_at=base, closed_at=base + timedelta(hours=2),
                    entry_order_id="777001", exit_order_id="777002", protective_order_id="777003"))
        s.add(Trade(account_id=acct.id, side="long", is_paper=True, entry_price=100.0, quantity=1.0, stop_loss_price=98.0, status="closed", pair="ETH/USDT",
                    opened_at=base, closed_at=base + timedelta(hours=1)))
        s.commit()
        account_id = acct.id
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    return dict(path=str(path), account_id=account_id, tmp=tmp_path)


class Windowed(tra.Recorder):
    """Serves only the rows inside the request's startTime..endTime, like the exchange does."""

    async def __call__(self, url, method="GET", headers=None, body=None):
        from urllib.parse import parse_qs, urlparse

        self.calls.append((method, url))
        q = parse_qs(urlparse(url).query)
        lo, hi = int(q.get("startTime", ["0"])[0]), int(q.get("endTime", [str(10**15)])[0])
        path = urlparse(url).path
        rows = self.responses.get(path, [])
        return [r for r in rows if lo <= int(r["time"]) <= hi]


def responses():
    return {
        "/fapi/v1/userTrades": [
            {"symbol": "BTCUSDT", "orderId": 777001, "side": "BUY", "price": "100.05", "qty": "1", "quoteQty": "100.05", "commission": "0.05", "commissionAsset": "USDT",
             "maker": False, "realizedPnl": "0", "time": T0 + 1000},
            {"symbol": "BTCUSDT", "orderId": 777002, "side": "SELL", "price": "102", "qty": "1", "quoteQty": "102", "commission": "0.051", "commissionAsset": "USDT",
             "maker": False, "realizedPnl": "1.95", "time": T0 + 2 * 3_600_000 - 500}],
        "/fapi/v1/income": [{"symbol": "BTCUSDT", "income": "-0.01", "asset": "USDT", "time": T0 + 60_000}],
    }


def test_the_export_end_to_end_is_sanitised_and_read_only(env, monkeypatch, capsys):
    rec = Windowed(responses())
    monkeypatch.setattr(ra, "build_client", lambda k, s: tra.make_client(rec))
    out = env["tmp"] / "bundle.json"
    code = le.main(["--database", env["path"], "--account-id", env["account_id"], "--days", "800", "--out", str(out)])
    printed = capsys.readouterr().out
    assert code == 0 and out.exists()
    text = out.read_text()
    assert hashlib.sha256(text.encode()).hexdigest() in printed
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    for needle in (API_KEY, API_SECRET, env["account_id"], "MBA-SECRETCODE", "owner-secret-mail", "555000111", "777001", "777002", "777003"):
        assert needle not in text and needle not in printed, needle
    trades, fills, income, _ = lg.load_bundle(text)
    assert len(trades) == 2 and len(fills) == 2 and len(income) == 2  # the one funding row is served for both income types in the fake
    live = [t for t in trades if not t.is_paper][0]
    assert live.entry_order_ref == fills[0].order_ref and live.exit_order_ref == fills[1].order_ref  # hashes agree across tables
    [row] = [e for e in lg.build_ledger(trades, fills, income) if e.klass == "normal"]
    assert row.reconciled and row.gross_pnl == pytest.approx(1.95)
    # every request was a GET to an allow-listed path
    for method, url in rec.calls:
        assert ra.request_allowed(url, method), (method, url)
    assert {re.sub(r"^https://[^/]+", "", u).split("?")[0] for _, u in rec.calls} <= ra.ALLOWED_GET_PATHS


def test_the_exporter_and_the_analysis_agree_on_the_format_and_the_hash():
    assert le.SCHEMA == lg.SCHEMA
    assert le.ref_hash('s1', 42) == lg.ref_hash('s1', 42) and le.ref_hash('s1', None) is None


def test_hashes_are_stable_within_an_export_and_differ_between_exports():
    assert lg.ref_hash("s1", 42) == lg.ref_hash("s1", 42) != lg.ref_hash("s2", 42)
    assert lg.ref_hash("s1", None) is None and "42" not in lg.ref_hash("s1", 42)


def test_the_output_file_is_never_overwritten(env, monkeypatch, capsys):
    out = env["tmp"] / "bundle.json"
    out.write_text("precious")
    monkeypatch.setattr(ra, "build_client", lambda k, s: pytest.fail("must not even connect"))
    assert le.main(["--database", env["path"], "--account-id", env["account_id"], "--out", str(out)]) == 2
    assert out.read_text() == "precious"
    with pytest.raises(FileExistsError):
        le.write_new_file(str(out), "x")


def test_a_refused_request_stops_the_export_and_writes_nothing(env, monkeypatch, capsys):
    class Evil(tra.Recorder):
        async def __call__(self, url, method="GET", headers=None, body=None):
            return await super().__call__("https://fapi.binance.com/fapi/v1/order", "POST", headers, body)

    rec = tra.Recorder(responses())

    async def bad_user_trades(self, *a, **k):
        raise ra.ReadOnlyViolation("x")

    monkeypatch.setattr(ra.ReadOnlyBinance, "user_trades", bad_user_trades)
    monkeypatch.setattr(ra, "build_client", lambda k, s: tra.make_client(rec))
    out = env["tmp"] / "b.json"
    assert le.main(["--database", env["path"], "--account-id", env["account_id"], "--days", "3650", "--out", str(out)]) == 4
    assert not out.exists()


def test_the_database_is_opened_read_only(env):
    rows = le.read_trades(env["path"], env["account_id"], 0)
    assert len(rows) == 2
    with pytest.raises(sqlite3.OperationalError):
        con = ra.open_readonly(env["path"])
        con.execute("delete from trades")


def test_a_leaked_credential_blocks_the_write(env, monkeypatch):
    bundle = {"schema": lg.SCHEMA, "notes": f"oops {API_KEY}", "trades": [], "fills": [], "income": []}
    with pytest.raises(ra.LeakDetected):
        ra.assert_clean(json.dumps(bundle), (API_KEY, API_SECRET))


# ------------------------------------------------------------------ the boundary in the source


def test_the_source_has_no_write_vocabulary_and_imports_no_trading_code():
    text = SRC.read_text()
    tree = ast.parse(text)
    doc_free = ast.unparse(tree).replace(ast.get_docstring(tree) or "", "")
    for token in tra.FORBIDDEN:
        assert token not in doc_free, token
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            imported.update(f"{'.' * node.level}{node.module or ''}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = ("trader", "scheduler", "lifecycle", "risk", "venue", "telemetry", "onboarding", "api", "db", "main", "notifications")
    for name in imported:
        for b in banned:
            assert not re.search(rf"(^|\.){b}(\.|$)", name), f"{name} pulls in {b}"


def test_nothing_else_imports_the_exporter_and_it_adds_no_endpoint():
    root = SRC.parent
    for path in root.rglob("*.py"):
        if path == SRC:
            continue
        for node in ast.walk(ast.parse(path.read_text())):  # code, not documentation
            names = [node.module or ""] + [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
            assert not any("ledger_export" in n for n in names), f"{path.name} imports the exporter"
    assert "ALLOWED_GET_PATHS" not in "".join(l for l in SRC.read_text().splitlines() if "ALLOWED_GET_PATHS =" in l)  # it defines none of its own


def test_repeated_rows_are_kept_once_so_a_fee_is_never_counted_twice():
    rows = [{"id": 1, "time": 5}, {"id": 1, "time": 5}, {"id": 2, "time": 6}]
    assert [r["id"] for r in le._dedupe(rows, lambda r: r["id"])] == [1, 2]

    class Repeating:
        async def income(self, kind, s, e):
            return [{"symbol": "BTCUSDT", "income": "-0.01", "asset": "USDT", "time": 9, "tranId": 77 if kind == "COMMISSION" else 78}] * 2

        async def user_trades(self, symbol, s, e):
            return [{"id": 1, "symbol": symbol, "orderId": 5, "time": 9, "price": "1", "qty": "1", "quoteQty": "1", "commission": "0.1", "side": "BUY"}] * 3

    import asyncio

    fills, income, errors = asyncio.run(le.collect(Repeating(), ["BTCUSDT"], 0, 10))
    assert len(fills) == 1 and len(income) == 2 and errors == []  # one funding row and one commission row, each once


def test_two_exports_of_the_same_data_use_different_references(env, monkeypatch):
    refs = []
    for name in ("a.json", "b.json"):
        rec = Windowed(responses())
        monkeypatch.setattr(ra, "build_client", lambda k, s, rec=rec: tra.make_client(rec))
        out = env["tmp"] / name
        assert le.main(["--database", env["path"], "--account-id", env["account_id"], "--days", "800", "--out", str(out)]) == 0
        refs.append({t["trade_ref"] for t in json.loads(out.read_text())["trades"]})
    assert refs[0].isdisjoint(refs[1])


def test_a_credential_value_anywhere_in_the_data_stops_the_export_and_nothing_is_written(env, monkeypatch, capsys):
    bad = responses()
    bad["/fapi/v1/userTrades"][0]["commissionAsset"] = API_KEY  # a value the exchange data should never contain
    rec = Windowed(bad)
    monkeypatch.setattr(ra, "build_client", lambda k, s: tra.make_client(rec))
    out = env["tmp"] / "leak.json"
    code = le.main(["--database", env["path"], "--account-id", env["account_id"], "--days", "800", "--out", str(out)])
    captured = capsys.readouterr()
    assert code == 1 and not out.exists()
    assert API_KEY not in captured.out and API_KEY not in captured.err


def test_the_documented_command_never_shows_the_encryption_key_and_names_the_own_account_rule():
    doc = ast.get_docstring(ast.parse(SRC.read_text()), clean=False)
    assert doc is not None
    assert "OWNER'S OWN trading account only" in doc and "customer's account" in doc
    # the key is only ever read from .env inside a command substitution, never written out as a value
    assert not re.search(r'ENCRYPTION_KEY=(?!"\$\()\S', doc), "a literal ENCRYPTION_KEY value or assignment appears in the docstring"
    assert 'ENCRYPTION_KEY="$(' in doc and "dotenv_values" in doc
    assert "--encryption-key" not in doc and "ENCRYPTION_KEY=..." not in doc
