r"""Owner-run, read-only export of the trade ledger bundle (design 4). NOT approved to run until the reviewer has reviewed it.

What it does: reads the bot's own trades for ONE account from the database (opened `mode=ro`), reads that account's fills
(userTrades) and commission and funding income from Binance through the SAME read-only boundary as `readonly_audit`
(a GET-only allow-list on five paths, enforced at the client's single HTTP entry point), and writes ONE sanitised JSON bundle
to a new file that the owner then sends for analysis.

What it cannot do: place, change or cancel an order, move funds, change a setting, or write to the database. It adds no
endpoint to the allow-list.

Sanitising: the account id, bot code and every order id are replaced by salted hashes (a fresh random salt per export, kept
nowhere); the bundle holds prices, quantities, fees, funding and times only. The bundle is scanned for forbidden field names
and for both credential values before it is written; the file is created new (never overwritten) with mode 600. The command
prints counts and the file's SHA-256, never the account id or any value from the bundle.

Whose account: the OWNER'S OWN trading account only. It reads that account's trades and fills, using that account's stored
credential. Do not point it at a customer's account (their key is not the owner's to use, and their trades are not the owner's
to export).

Run by the owner, as the app user, from the app directory, once the reviewer has approved this file and its checksum.
The encryption key is never typed, never put on the command line and never printed. This module does not read the app's .env
itself, so the shell supplies the key to this one command only, read from .env by the same parser the app uses:

    cd /opt/mba-bot
    ENCRYPTION_KEY="$(.venv/bin/python -c 'from dotenv import dotenv_values; print(dotenv_values(".env")["ENCRYPTION_KEY"])')" \
        .venv/bin/python -m mba_bot.ledger_export --database /opt/mba-bot/mba_bot.db --account-id <id or bot code> \
        --days 90 --out /home/botops/ledger_bundle.json

* The key exists only in this command's environment. The shell history keeps the command text above, not the key.
* Do not run it with `set -x`, `bash -x` or any tracing, do not `echo` the variable, and do not paste the command output of
  the dotenv one-liner anywhere. To check that the key was found without showing it, run the same one-liner as
  `test -n "$(...)" && echo found`.
* The same applies to any other secret in .env: none is needed here.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timezone

from . import readonly_audit as ra
from .exchange import safe_error_message

MAX_SYMBOLS = 12
# Kept in step with research/ledger.py by a test; duplicated here because the service side must not import the research package.
SCHEMA = "mba-ledger-bundle/1"


def ref_hash(salt: str, value) -> str | None:
    """Stable, non-reversible reference for an id inside one export (a fresh random salt per export)."""
    if value is None or value == "":
        return None
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]


def _ms(value) -> int | None:
    if not value:
        return None
    text = str(value).replace(" ", "T").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def read_trades(db_path: str, account_id: str, since_ms: int) -> list[dict]:
    """The account's trades since `since_ms`, read-only. Only the columns named here are selected."""
    con = ra.open_readonly(db_path)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        join = "left join trade_telemetry tt on tt.trade_id = t.id" if "trade_telemetry" in tables else ""
        sig = "tt.signal_price" if join else "null"
        rows = con.execute(
            "select t.id, t.pair, t.side, t.is_paper, t.status, t.close_reason, t.opened_at, t.closed_at, t.entry_price, t.exit_price, "
            f"t.stop_loss_price, t.quantity, t.pnl, t.entry_order_id, t.exit_order_id, t.protective_order_id, {sig} as signal_price "
            f"from trades t {join} where t.account_id = ? order by t.opened_at", (account_id,)).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        opened = _ms(r["opened_at"])
        if opened is not None and opened >= since_ms - 7 * ra.DAY_MS:
            out.append(dict(r))
    return out


def build_bundle(trade_rows: list[dict], fills: list[dict], income: list[dict], *, salt: str, window: dict) -> dict:
    h = lambda v: ref_hash(salt, v)  # noqa: E731
    trades = []
    for r in trade_rows:
        trades.append({
            "trade_ref": h(r["id"]), "pair": r["pair"], "side": r["side"], "is_paper": bool(r["is_paper"]), "status": r["status"],
            "close_reason": r["close_reason"], "opened_ms": _ms(r["opened_at"]), "closed_ms": _ms(r["closed_at"]),
            "signal_price": r.get("signal_price"), "entry_price": r["entry_price"], "exit_price": r["exit_price"],
            "stop_price": r["stop_loss_price"], "quantity": r["quantity"], "pnl": r["pnl"],
            "entry_order_ref": h(r["entry_order_id"]), "exit_order_ref": h(r["exit_order_id"]), "stop_order_ref": h(r["protective_order_id"]),
        })
    return {
        "schema": SCHEMA, "exported_at_ms": int(time.time() * 1000), "window": window,
        "salt_id": hashlib.sha256(salt.encode()).hexdigest()[:8],
        "trades": trades,
        "fills": [{
            "time_ms": int(f["time"]), "symbol": f["symbol"], "side": f["side"], "price": float(f["price"]), "qty": float(f["qty"]),
            "quote_qty": float(f["quoteQty"]), "commission": float(f["commission"]), "commission_asset": f.get("commissionAsset", "USDT"),
            "maker": bool(f.get("maker", False)), "realized_pnl": float(f.get("realizedPnl", 0.0)), "order_ref": h(f.get("orderId")),
        } for f in fills],
        "income": [{
            "time_ms": int(i["time"]), "symbol": i.get("symbol", ""), "kind": i["incomeType"] if "incomeType" in i else i.get("kind", ""),
            "amount": float(i["income"]), "asset": i.get("asset", "USDT"),
        } for i in income],
    }


def _dedupe(rows: list[dict], key_fn) -> list[dict]:
    """Pagination and window edges can repeat a row; a repeated fill would double a fee, so each row is kept once."""
    seen, out = set(), []
    for r in rows:
        k = key_fn(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


async def collect(client: "ra.ReadOnlyBinance", symbols: list[str], start_ms: int, end_ms: int) -> tuple[list[dict], list[dict], list[str]]:
    fills: list[dict] = []
    income: list[dict] = []
    errors: list[str] = []
    for kind in ("COMMISSION", "FUNDING_FEE"):
        try:
            for row in await client.income(kind, start_ms, end_ms):
                income.append({**row, "incomeType": kind})
        except ra.ReadOnlyViolation:
            raise
        except Exception as exc:  # noqa: BLE001 - category only
            errors.append(f"income {kind}: {safe_error_message(exc)}")
    for s in symbols:
        try:
            fills += await client.user_trades(s, start_ms, end_ms)
        except ra.ReadOnlyViolation:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"userTrades {s}: {safe_error_message(exc)}")
    fills = _dedupe(fills, lambda r: ("id", r["id"]) if r.get("id") is not None else (r.get("orderId"), r.get("time"), r.get("price"), r.get("qty"), r.get("side")))
    income = _dedupe(income, lambda r: ("tran", r["tranId"]) if r.get("tranId") is not None else (r.get("time"), r.get("symbol"), r.get("incomeType"), r.get("income")))
    return fills, income, errors


def write_new_file(path: str, text: str) -> str:
    """Creates the file (never overwrites), mode 600. Returns its SHA-256."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mba_bot.ledger_export", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", required=True)
    ap.add_argument("--account-id", required=True, help="the trading account id or bot code; no default")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", required=True, help="a NEW file; an existing file is never overwritten")
    args = ap.parse_args(argv)
    if os.path.exists(args.out):
        sys.stderr.write("refused: the output file already exists\n")
        return 2
    try:
        key, secret, facts = ra.load_credential(args.database, args.account_id, encryption_key=os.environ.get("ENCRYPTION_KEY"))
        client = ra.build_client(key, secret)
        secrets_ = (key, secret)
        key = secret = None
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - args.days * ra.DAY_MS
        trade_rows = read_trades(args.database, facts["account_id"], start_ms)
        symbols = sorted({r["pair"].replace("/", "") for r in trade_rows if r["pair"] and not r["is_paper"]})[:MAX_SYMBOLS]

        async def go():
            try:
                return await collect(client, symbols, start_ms, end_ms)
            finally:
                await client.close()

        fills, income, errors = asyncio.run(go())
        bundle = build_bundle(trade_rows, fills, income, salt=secrets.token_hex(16), window={"start_ms": start_ms, "end_ms": end_ms, "days": args.days})
        text = ra.assert_clean(json.dumps(bundle, indent=1), secrets_)
        digest = write_new_file(args.out, text)
        sys.stdout.write(f"wrote {args.out}\nsha256 {digest}\ntrades {len(bundle['trades'])} fills {len(bundle['fills'])} income rows {len(bundle['income'])} symbols {len(symbols)}\n")
        if errors:
            sys.stdout.write("some reads failed (category only): " + "; ".join(errors) + "\n")
        return 0 if not errors else 3
    except ra.ReadOnlyViolation:
        sys.stderr.write("refused: a request outside the read-only allow-list was attempted; nothing was sent\n")
        return 4
    except Exception as exc:  # noqa: BLE001 - never echo exception text
        sys.stderr.write(f"export failed: {str(exc) if isinstance(exc, (ra.AuditInputError,)) else safe_error_message(exc)}\n")
        return 1
    finally:
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())
