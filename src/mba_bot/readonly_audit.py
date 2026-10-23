"""Read-only fee and funding audit of one Binance USD-M Futures account.

Purpose: replace the research backtester's ASSUMED costs (5 bp taker fee, funding from public history) with what the account
actually paid, so a cost multiple can be read off the evidence instead of guessed. It reports; it changes nothing.

What it can do (the whole surface, enforced twice):
  * read the account's commission rates, fee tier and fee-discount status;
  * read the income history (commissions, funding payments) and the fill history.
What it cannot do: place, change or cancel an order, move funds, withdraw, alter leverage or margin mode, or write to the
database.

How the boundary is enforced
  1. `ReadOnlyBinance` exposes five named methods and nothing else. The exchange client is private to it.
  2. A guard on the client's single HTTP chokepoint refuses every request that is not a GET to one of five allow-listed paths
     on the production Futures host. A mistake in this file, or in a library call made from it, cannot reach any other endpoint;
     the refusal happens before anything is sent, and signing only happens inside the request, so a refused call is never signed.
Credentials
  The stored credential is read with the same path the trading service uses (the encrypted row, then the operator's
  ENCRYPTION_KEY). The database is opened `mode=ro`. The key and secret live in one local variable on the way to the client
  constructor and are never printed, logged, written or returned. All output passes a leak check against both values.

Nothing here is imported by the trading service. It is run by hand, once the owner has authorised it:

    ENCRYPTION_KEY=... python -m mba_bot.readonly_audit --database /opt/mba-bot/mba_bot.db --account-id <id or bot code> --days 90
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from . import log_safety
from .crypto_utils import decrypt
from .exchange import build_exchange_client, safe_error_message

ALLOWED_HOST = "fapi.binance.com"
ALLOWED_GET_PATHS = frozenset({"/fapi/v1/commissionRate", "/fapi/v1/income", "/fapi/v1/userTrades", "/fapi/v1/feeBurn", "/fapi/v2/account"})
DAY_MS = 86_400_000
MODEL_TAKER_BPS = 5.0  # the research cost model's assumption, for comparison only
MAX_PAGES = 40  # hard bound on pagination


class ReadOnlyViolation(RuntimeError):
    """A request outside the read-only allow-list was attempted. Nothing was sent."""


class AuditInputError(Exception):
    """A problem with the operator's inputs. Its text is written by this module, so it is safe to show."""


class LeakDetected(RuntimeError):
    """Output contained a credential value. It was not emitted."""


# ------------------------------------------------------------------ the boundary


def request_allowed(url: str, method: str) -> bool:
    parsed = urlparse(url)
    return (str(method).upper() == "GET" and parsed.scheme == "https" and (parsed.hostname or "").lower() == ALLOWED_HOST
            and parsed.path in ALLOWED_GET_PATHS)


def install_guard(client) -> None:
    """Wrap the client's one HTTP entry point. Every ccxt call (public, private, implicit or unified) ends in `fetch`."""
    original = client.fetch

    async def guarded(url, method="GET", headers=None, body=None):
        if not request_allowed(url, method):
            raise ReadOnlyViolation(f"refused {str(method).upper()} {urlparse(url).path}: not in the read-only allow-list")
        if body:  # a GET carries its parameters in the query string; a body would be a write
            raise ReadOnlyViolation("refused a request with a body")
        return await original(url, method, headers, body)

    client.fetch = guarded


class ReadOnlyBinance:
    """The only object the audit talks to. Public methods are exactly the five below."""

    __slots__ = ("_c",)

    def __init__(self, client):
        install_guard(client)
        self._c = client

    async def commission_rate(self, symbol: str) -> dict:
        r = await self._c.fapiPrivateGetCommissionRate({"symbol": symbol})
        return {"symbol": r.get("symbol", symbol), "maker": float(r["makerCommissionRate"]), "taker": float(r["takerCommissionRate"])}

    async def account_fee_tier(self) -> dict:
        r = await self._c.fapiPrivateV2GetAccount({})
        return {"fee_tier": r.get("feeTier"), "can_trade": r.get("canTrade"), "multi_assets_margin": r.get("multiAssetsMargin")}

    async def fee_burn_enabled(self) -> bool | None:
        r = await self._c.fapiPrivateGetFeeBurn({})
        v = r.get("feeBurn")
        return None if v is None else (v is True or str(v).lower() == "true")

    async def income(self, income_type: str, start_ms: int, end_ms: int) -> list[dict]:
        if income_type not in ("COMMISSION", "FUNDING_FEE"):
            raise ValueError("only COMMISSION and FUNDING_FEE income is read")
        rows: list[dict] = []
        cursor = start_ms
        for _ in range(MAX_PAGES):
            page = await self._c.fapiPrivateGetIncome({"incomeType": income_type, "startTime": cursor, "endTime": end_ms, "limit": 1000})
            if not page:
                break
            rows += page
            newest = max(int(r["time"]) for r in page)
            if len(page) < 1000 or newest < cursor:
                break
            cursor = newest + 1
        return rows

    async def user_trades(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        # Binance allows at most 7 days per userTrades request
        rows: list[dict] = []
        t = start_ms
        while t < end_ms:
            stop = min(t + 7 * DAY_MS - 1, end_ms)
            cursor = t
            for _ in range(MAX_PAGES):
                page = await self._c.fapiPrivateGetUserTrades({"symbol": symbol, "startTime": cursor, "endTime": stop, "limit": 1000})
                if not page:
                    break
                rows += page
                newest = max(int(r["time"]) for r in page)
                if len(page) < 1000 or newest < cursor:
                    break
                cursor = newest + 1
            t = stop + 1
        return rows

    async def close(self) -> None:
        await self._c.close()


# ------------------------------------------------------------------ credentials (same path as the trading service, read-only)


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise AuditInputError(f"no database at {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_credential(db_path: str | Path, account_ref: str, *, encryption_key: str | None) -> tuple[str, str, dict]:
    """Returns (api_key, api_secret, public_facts). Only the facts may be shown to anyone."""
    con = open_readonly(db_path)
    try:
        rows = con.execute(
            "SELECT a.id AS id, a.bot_code AS bot_code, a.market_type AS market_type, c.environment AS environment, c.exchange AS exchange,"
            " c.encrypted_api_key AS k, c.encrypted_api_secret AS s FROM api_credentials c JOIN trading_accounts a ON a.id = c.account_id"
            " WHERE a.id = ? OR a.bot_code = ?", (account_ref, account_ref)).fetchall()
    finally:
        con.close()
    if len(rows) != 1:
        raise AuditInputError("expected exactly one account matching that id or bot code")
    row = rows[0]
    if row["exchange"] != "binance" or row["market_type"] != "futures":
        raise AuditInputError("this audit reads Binance USD-M Futures accounts only")
    if row["environment"] != "mainnet":
        raise AuditInputError("this audit reads a mainnet account only; the stored credential is not tagged mainnet")
    return (decrypt(row["k"], key=encryption_key), decrypt(row["s"], key=encryption_key),
            {"account_id": row["id"], "bot_code": row["bot_code"], "market_type": row["market_type"], "environment": row["environment"]})


def build_client(api_key: str, api_secret: str) -> ReadOnlyBinance:
    log_safety.quiet_http_client_logs()
    client = build_exchange_client(exchange="binance", market_type="futures", api_key=api_key, api_secret=api_secret, sandbox=False)
    return ReadOnlyBinance(client)


def assert_clean(text: str, secrets: tuple[str, ...]) -> str:
    for s in secrets:
        if s and len(s) >= 6 and s in text:
            raise LeakDetected("output contained a credential value and was not emitted")
    return text


# ------------------------------------------------------------------ analysis (pure)


def summarize_income(commission_rows: list[dict], funding_rows: list[dict]) -> dict:
    by_sym: dict[str, dict] = defaultdict(lambda: {"commission_usdt": 0.0, "funding_usdt": 0.0, "funding_events": 0, "commission_events": 0})
    other_assets: dict[str, float] = defaultdict(float)
    for r in commission_rows:
        amt = float(r["income"])
        if r.get("asset", "USDT") == "USDT":
            by_sym[r.get("symbol", "")]["commission_usdt"] += amt
        else:
            other_assets[r.get("asset", "?")] += amt  # not converted: reported as is
        by_sym[r.get("symbol", "")]["commission_events"] += 1
    for r in funding_rows:
        if r.get("asset", "USDT") == "USDT":
            by_sym[r.get("symbol", "")]["funding_usdt"] += float(r["income"])
            by_sym[r.get("symbol", "")]["funding_events"] += 1
    return {"by_symbol": dict(by_sym), "commission_in_other_assets": dict(other_assets),
            "total_commission_usdt": sum(v["commission_usdt"] for v in by_sym.values()), "total_funding_usdt": sum(v["funding_usdt"] for v in by_sym.values())}


def summarize_fills(trades: list[dict]) -> dict:
    """Effective fee in basis points of traded notional, per symbol and overall, split by maker/taker. Commission paid in a
    non-USDT asset (for example BNB) is counted separately and not converted."""
    acc: dict[str, dict] = defaultdict(lambda: {"fills": 0, "notional": 0.0, "fee_usdt": 0.0, "taker_notional": 0.0, "taker_fee_usdt": 0.0, "non_usdt_fills": 0})
    for t in trades:
        a = acc[t["symbol"]]
        q = float(t["quoteQty"])
        a["fills"] += 1
        a["notional"] += q
        if t.get("commissionAsset", "USDT") != "USDT":
            a["non_usdt_fills"] += 1
            continue
        fee = float(t["commission"])
        a["fee_usdt"] += fee
        if not t.get("maker", False):
            a["taker_notional"] += q
            a["taker_fee_usdt"] += fee
    out = {}
    for sym, a in acc.items():
        out[sym] = {**a, "effective_fee_bps": (a["fee_usdt"] / a["notional"] * 1e4) if a["notional"] else None,
                    "effective_taker_bps": (a["taker_fee_usdt"] / a["taker_notional"] * 1e4) if a["taker_notional"] else None}
    n, fee = sum(a["notional"] for a in acc.values()), sum(a["fee_usdt"] for a in acc.values())
    tn, tf = sum(a["taker_notional"] for a in acc.values()), sum(a["taker_fee_usdt"] for a in acc.values())
    return {"by_symbol": out, "fills": sum(a["fills"] for a in acc.values()), "notional": n, "effective_fee_bps": (fee / n * 1e4) if n else None,
            "effective_taker_bps": (tf / tn * 1e4) if tn else None}


def compare_to_model(fills: dict, rates: dict[str, dict], model_taker_bps: float = MODEL_TAKER_BPS) -> dict:
    taker_bps = [r["taker"] * 1e4 for r in rates.values()]
    stated = statistics.fmean(taker_bps) if taker_bps else None
    realised = fills.get("effective_taker_bps")
    return {"model_taker_bps": model_taker_bps, "stated_taker_bps": stated, "realised_taker_bps": realised,
            "stated_over_model": (stated / model_taker_bps) if stated is not None else None,
            "realised_over_model": (realised / model_taker_bps) if realised is not None else None,
            "note": "Slippage is not visible to the exchange record. This compares the FEE leg of the research cost model only."}


# ------------------------------------------------------------------ the audit


async def run_audit(client: ReadOnlyBinance, symbols: list[str], *, days: int, now_ms: int | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    start = now_ms - days * DAY_MS
    rates, errors = {}, {}
    for s in symbols:
        try:
            rates[s] = await client.commission_rate(s)
        except ReadOnlyViolation:
            raise
        except Exception as exc:  # noqa: BLE001 - category only, never the exception text
            errors[f"commission_rate:{s}"] = safe_error_message(exc)
    out: dict = {"window_days": days, "start_ms": start, "end_ms": now_ms, "symbols": symbols, "commission_rates": rates, "errors": errors}
    for name, call in (("account", client.account_fee_tier()), ("fee_burn_enabled", client.fee_burn_enabled())):
        try:
            out[name] = await call
        except Exception as exc:  # noqa: BLE001
            errors[name] = safe_error_message(exc)
    try:
        commission = await client.income("COMMISSION", start, now_ms)
        funding = await client.income("FUNDING_FEE", start, now_ms)
        out["income"] = summarize_income(commission, funding)
    except ReadOnlyViolation:
        raise
    except Exception as exc:  # noqa: BLE001
        errors["income"] = safe_error_message(exc)
    trades: list[dict] = []
    for s in symbols:
        try:
            trades += await client.user_trades(s, start, now_ms)
        except ReadOnlyViolation:
            raise
        except Exception as exc:  # noqa: BLE001
            errors[f"user_trades:{s}"] = safe_error_message(exc)
    out["fills"] = summarize_fills(trades)
    out["versus_research_model"] = compare_to_model(out["fills"], rates)
    return out


def render(res: dict, facts: dict) -> str:
    def f(x, d=2):
        return "n/a" if x is None else f"{x:.{d}f}"

    m, fills = res["versus_research_model"], res["fills"]
    lines = [f"Read-only fee and funding audit, last {res['window_days']} days, account {facts.get('bot_code') or facts.get('account_id')} ({facts.get('market_type')}, {facts.get('environment')})",
             f"Fee tier {res.get('account', {}).get('fee_tier')}; BNB fee discount enabled: {res.get('fee_burn_enabled')}", "",
             f"Stated taker fee {f(m['stated_taker_bps'])} bp, realised taker fee {f(m['realised_taker_bps'])} bp, research model {f(m['model_taker_bps'])} bp "
             f"(stated {f(m['stated_over_model'])}x, realised {f(m['realised_over_model'])}x the model).", f"Fills read: {fills['fills']}, notional {f(fills['notional'], 0)} USDT."]
    inc = res.get("income")
    if inc:
        lines += ["", f"Commission paid (USDT): {f(inc['total_commission_usdt'], 4)}; funding paid/received (USDT, negative = paid): {f(inc['total_funding_usdt'], 4)}"]
        if inc["commission_in_other_assets"]:
            lines.append("Commission also paid in other assets (not converted): " + ", ".join(f"{a} {v:.6f}" for a, v in inc["commission_in_other_assets"].items()))
        lines += ["", "| symbol | fills | notional | effective fee bp | taker bp | commission | funding events | funding |", "|---|---|---|---|---|---|---|---|"]
        for sym, v in sorted(fills["by_symbol"].items()):
            i = inc["by_symbol"].get(sym, {})
            lines.append(f"| {sym} | {v['fills']} | {f(v['notional'], 0)} | {f(v['effective_fee_bps'])} | {f(v['effective_taker_bps'])} | {f(i.get('commission_usdt'), 4)} | {i.get('funding_events', 0)} | {f(i.get('funding_usdt'), 4)} |")
    if res["errors"]:
        lines += ["", "Some reads failed (category only): " + "; ".join(f"{k}: {v}" for k, v in res["errors"].items())]
    lines += ["", m["note"], "Nothing was written to the exchange or the database."]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ command line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.readonly_audit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", required=True, help="path to the SQLite file (opened read-only)")
    ap.add_argument("--account-id", required=True, help="the trading account id or bot code; no default, no guessing")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    key = secret = None
    try:
        key, secret, facts = load_credential(args.database, args.account_id, encryption_key=os.environ.get("ENCRYPTION_KEY"))
        client = build_client(key, secret)
        secrets = (key, secret)
        key = secret = None  # the client holds them now; drop our references

        async def go():
            try:
                return await run_audit(client, [s.strip().upper() for s in args.symbols.split(",") if s.strip()], days=args.days)
            finally:
                await client.close()

        res = asyncio.run(go())
        text = json.dumps({"facts": facts, **res}, indent=1, default=str) if args.json else render(res, facts)
        sys.stdout.write(assert_clean(text, secrets))
        return 0
    except ReadOnlyViolation:
        sys.stderr.write("refused: a request outside the read-only allow-list was attempted; nothing was sent\n")
        return 4
    except Exception as exc:  # noqa: BLE001 - never echo exception text: it can carry request details
        sys.stderr.write(f"audit failed: {str(exc) if isinstance(exc, AuditInputError) else safe_error_message(exc)}\n")
        return 1
    finally:
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())
