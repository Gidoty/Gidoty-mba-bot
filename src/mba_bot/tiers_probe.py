"""Read-only probe of Binance's leverage brackets for ONE account and pair, run by the owner ON THE SERVER.

    cd /opt/mba-bot && PYTHONPATH=src .venv/bin/python -m mba_bot.tiers_probe                          (lists the active Futures accounts)
    cd /opt/mba-bot && PYTHONPATH=src .venv/bin/python -m mba_bot.tiers_probe --account 27624db9 [--pair XRP/USDT]

It answers one question: what does Binance send for this symbol's leverage brackets, and does the entry check's own reader understand it? It reads the brackets through the same building blocks the entry check
uses (exchange._read_once, once through ccxt's parser and once through the raw endpoint, then exchange.read_maintenance_tiers as the entry check calls it) and prints ONLY: which attempt worked, the shape of
Binance's answer (list, single object, mapping, length), the number of brackets, the first and last bracket's floor, cap and rate, the first failing plausibility check, and the cause when it failed. It never prints a
key, a signature, a header, a balance or an e-mail.

What it does to Binance: it uses the account's STORED read credentials (decrypted inside the existing code path, never shown) and sends one public read (GET exchange information) and up to four SIGNED READ requests (GET
leverageBracket: once through ccxt, once raw, then the entry check's own read, which may try twice). It places, changes and cancels nothing, and it cannot: the client is wrapped so that only a short list of read methods can be called, and any other method (an order, a leverage or margin
change, any POST, PUT or DELETE endpoint) raises before a request is made. It needs an explicit --account. It opens the database read-only. Exit codes: 0 printed, 2 could not start or no such account."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import exchange
from .config import load_settings
from .models import TradingAccount
from .restart_gate import DEFAULT_URL, _engine
from .risk import tiers_problem

# The ONLY client attributes the probe can reach. Everything else raises.
ALLOWED = frozenset({"market", "load_markets", "fetch_leverage_tiers", "fapiPrivateGetLeverageBracket", "parse_market_leverage_tiers", "close"})


class ReadOnlyViolation(RuntimeError):
    pass


class ReadOnlyClient:
    """Wraps a ccxt client: only the ALLOWED read methods are reachable; anything else raises ReadOnlyViolation before a request can be made."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        if name not in ALLOWED:
            raise ReadOnlyViolation(f"the tiers probe may not call {name}")
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        raise ReadOnlyViolation("the tiers probe may not change the client")


def short(account_id: str) -> str:
    return account_id[:8]


def _bracket_line(t) -> str:
    return f"floor {t.floor:,.0f}, cap {t.cap:,.0f}, rate {t.rate:g}"


def summarize(tiers) -> list[str]:
    if not tiers:
        return ["brackets: none"]
    ordered = sorted(tiers, key=lambda t: t.floor)
    problem = tiers_problem(tiers)
    return [f"brackets: {len(tiers)}", f"first bracket: {_bracket_line(ordered[0])}", f"last bracket: {_bracket_line(ordered[-1])}",
            f"plausibility check: {'passes' if problem is None else 'FAILS: ' + problem}"]


def account_lines(session: Session) -> list[str]:
    rows = session.scalars(select(TradingAccount).where(TradingAccount.is_active.is_(True), TradingAccount.market_type == "futures").order_by(TradingAccount.id)).all()
    out = ["active Futures accounts (short id, pair, pair selection, live key on file, strategies opted in):"]
    for a in rows:
        extra = (a.extra_strategies or "").strip() or "-"
        out.append(f"  {short(a.id)}  {a.pair:<10} {a.pair_selection_mode:<6} live key {'yes' if a.has_mainnet_credential else 'no'}  strategies {extra}")
    if len(out) == 1:
        out.append("  none")
    out.append("run again with --account <short id> [--pair XRP/USDT] to read that pair's brackets")
    return out


async def probe(client, pair: str) -> list[str]:
    """The three reads, as lines of text. `client` must be a ReadOnlyClient (or a test double with the same methods)."""
    symbol = exchange.exchange_symbol(pair, "futures")
    out = [f"symbol {symbol}"]
    try:
        await client.load_markets()  # public exchange information; also needed to resolve the symbol
    except Exception as exc:  # noqa: BLE001
        return out + [f"could not load Binance's market list: {type(exc).__name__}: {exchange.scrub_exchange_text(exc)}"]
    for label, raw in (("attempt 1: through ccxt's own parser", False), ("attempt 2: raw endpoint (reads either answer shape)", True)):
        tiers, cause, detail = await exchange._read_once(client, symbol, raw=raw)
        if tiers is not None:
            out += [f"{label}: WORKED ({detail})"] + ["  " + line for line in summarize(tiers)]
        else:
            out += [f"{label}: FAILED, cause {cause}: {detail}"]
    r = await exchange.read_maintenance_tiers(client, symbol, retry=True)
    out.append(f"the entry check's own read: {'WORKED' if r.tiers is not None else 'FAILED'} (source {r.source}, attempts {r.attempts}" + (f", cause {r.cause}" if r.cause else "") + ")")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tiers_probe")
    ap.add_argument("--account", help="short account id (first 8 characters or more); without it only the account list is printed")
    ap.add_argument("--pair", help="e.g. XRP/USDT (default: the account's own pair)")
    args = ap.parse_args(argv)
    try:
        url = os.environ.get("DATABASE_URL", DEFAULT_URL)
        if url.startswith("sqlite") and ":memory:" not in url:
            path = url.split("///", 1)[-1].split("?", 1)[0]
            if path and not os.path.exists(path):
                raise FileNotFoundError("database file not found")
        settings = load_settings()
        engine = _engine(url)
        with Session(engine) as session:
            if not args.account:
                print("\n".join(account_lines(session)))
                return 0
            if len(args.account) < 4:
                print("tiers probe: give at least 4 characters of the account id", file=sys.stderr)
                return 2
            matches = session.scalars(select(TradingAccount).where(TradingAccount.id.like(args.account + "%"))).all()
            if len(matches) != 1:
                print(f"tiers probe: {len(matches)} accounts match that id; give more characters", file=sys.stderr)
                return 2
            account = matches[0]
            if account.market_type != "futures" or not account.has_mainnet_credential:
                print("tiers probe: that account is not a live Futures account with a stored Binance key", file=sys.stderr)
                return 2
            from .trader import _resolve_credentials  # the existing decryption path; the key never leaves it

            api_key, api_secret = _resolve_credentials(account, settings, is_paper=False)
            if api_key is None:
                print("tiers probe: no usable stored credential for that account", file=sys.stderr)
                return 2
            pair = (args.pair or account.pair).strip().upper()
            print(f"tiers probe for account {short(account.id)}, pair {pair}: sends 1 public and up to 4 signed READ requests to Binance with this account's stored read credentials; places, changes and cancels nothing.")
            inner = exchange.build_exchange_client(exchange="binance", market_type="futures", api_key=api_key, api_secret=api_secret, sandbox=False)
            client = ReadOnlyClient(inner)

            async def run():
                try:
                    return await probe(client, pair)
                finally:
                    await inner.close()

            print("\n".join(asyncio.run(run())))
        engine.dispose()
        return 0
    except Exception as exc:  # noqa: BLE001 - class name only: never the message, which could carry request details
        print(f"tiers probe: cannot run ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
